"""Login: the customer id comes only from a verified JWT; bad tokens are refused, never downgraded silently."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import api, auth, config

BRANCH = {"branch_id": "64a0000000000000000000aa", "name": "Gulberg", "city": "Lahore"}
SECRET = "test-secret-for-hs256-that-is-long-enough"


def token(claims=None, key=SECRET, alg="HS256", **extra):
    body = {"sub": "cust-1", "exp": int(time.time()) + 600, **(claims or {}), **extra}
    return jwt.encode(body, key, algorithm=alg)


@pytest.fixture
def client(monkeypatch):
    calls = []

    def fake_chat(message, *, thread_id, branch_id, user_id=None, cart=None, payment_method=None):
        calls.append({"thread_id": thread_id, "user_id": user_id})
        return {"answer": "ok", "items": [], "suggestions": [], "cart": None, "handoff": False, "as_of": "t"}

    monkeypatch.setattr(api, "chat", fake_chat)
    monkeypatch.setattr(api, "branches", lambda: [BRANCH])
    monkeypatch.setattr(api, "limiter", api.RateLimiter(100))
    for name, value in dict(JWT_SECRET=SECRET, JWT_PUBLIC_KEY="", JWT_JWKS_URL="", JWT_ALGORITHMS=[],
                            JWT_USER_CLAIMS=["sub", "id", "_id", "userId", "customerId"], JWT_AUDIENCE="", JWT_ISSUER="", AUTH_REQUIRED=False).items():
        monkeypatch.setattr(config, name, value)
    c = TestClient(api.app)
    c.calls = calls
    return c


def post(client, tok=None, **body):
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    return client.post("/api/chat", json={"message": "where is my order", "branch_id": BRANCH["branch_id"], **body},
                       headers=headers)


def test_valid_token_sets_customer(client):
    r = post(client, token())
    assert r.status_code == 200 and client.calls[0]["user_id"] == "cust-1"
    assert client.calls[0]["thread_id"].split(":")[1] == "cust-1"


def test_no_token_is_guest(client):
    assert post(client).status_code == 200 and client.calls[0]["user_id"] is None


@pytest.mark.parametrize("bad, code", [
    (lambda: token(exp=int(time.time()) - 3600), "token_expired"),
    (lambda: token(key="some-other-secret-that-is-also-long"), "invalid_token"),  # forged signature
    (lambda: "not.a.jwt", "invalid_token"),
    (lambda: jwt.encode({"sub": "cust-1"}, SECRET, algorithm="HS256"), "invalid_token"),  # no exp
    (lambda: jwt.encode({"sub": "x", "exp": int(time.time()) + 600}, None, algorithm="none"), "invalid_token"),
])
def test_bad_tokens_rejected(client, bad, code):
    r = post(client, bad())
    assert r.status_code == 401 and r.json()["detail"] == code and not client.calls


def test_body_cannot_override_identity(client):
    post(client, token(), user_id="victim", customer_id="victim")
    assert client.calls[0]["user_id"] == "cust-1"


def test_custom_claim_path(client, monkeypatch):
    monkeypatch.setattr(config, "JWT_USER_CLAIMS", ["user.id"])
    assert post(client, token({"user": {"id": "abc123"}})).status_code == 200
    assert client.calls[0]["user_id"] == "abc123"
    assert post(client, token()).status_code == 401  # claim missing


def test_audience_and_issuer(client, monkeypatch):
    monkeypatch.setattr(config, "JWT_AUDIENCE", "chatbot")
    monkeypatch.setattr(config, "JWT_ISSUER", "cheezious-app")
    assert post(client, token(aud="chatbot", iss="cheezious-app")).status_code == 200
    assert post(client, token(aud="other", iss="cheezious-app")).status_code == 401
    assert post(client, token(aud="chatbot", iss="evil")).status_code == 401


def test_rs256_public_key(client, monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.public_key().public_bytes(serialization.Encoding.PEM,
                                            serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setattr(config, "JWT_SECRET", "")
    monkeypatch.setattr(config, "JWT_PUBLIC_KEY", pem)
    assert post(client, token(key=private, alg="RS256")).status_code == 200
    # an HS256 token "signed" with the public key text must not pass (algorithm confusion)
    assert post(client, token(key=SECRET, alg="HS256")).status_code == 401


def test_auth_required_rejects_guests(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_REQUIRED", True)
    r = post(client)
    assert r.status_code == 401 and r.json()["detail"] == "login_required"


def test_unconfigured_auth_treats_token_as_guest(client, monkeypatch):
    monkeypatch.setattr(config, "JWT_SECRET", "")
    assert post(client, token()).status_code == 200 and client.calls[0]["user_id"] is None


def test_session_is_private_to_its_owner(client, monkeypatch):
    seen = []

    class FakeGraph:
        def get_state(self, cfg):
            seen.append(cfg["configurable"]["thread_id"])
            return type("S", (), {"values": {}})()

    monkeypatch.setattr(api, "graph", lambda: FakeGraph())
    sid = "b" * 32
    client.get(f"/api/session?branch_id={BRANCH['branch_id']}&session_id={sid}",
               headers={"Authorization": f"Bearer {token()}"})
    client.get(f"/api/session?branch_id={BRANCH['branch_id']}&session_id={sid}")  # same id, no login
    assert seen == [f"{BRANCH['branch_id']}:cust-1:{sid}", f"{BRANCH['branch_id']}:guest:{sid}"]


def test_per_user_rate_limit(client, monkeypatch):
    monkeypatch.setattr(api, "limiter", api.RateLimiter(2))
    codes = [post(client, token()).status_code for _ in range(3)]
    assert codes[-1] == 429


def test_default_claim_fallback_order(client):
    # typical Express payloads: {id: ...} or {_id: ...} with no sub
    no_sub = {"exp": int(time.time()) + 600}
    assert post(client, jwt.encode({**no_sub, "id": "uuid-from-id"}, SECRET, algorithm="HS256")).status_code == 200
    assert post(client, jwt.encode({**no_sub, "_id": "uuid-from-_id", "userId": "other"}, SECRET,
                                   algorithm="HS256")).status_code == 200
    assert [c["user_id"] for c in client.calls] == ["uuid-from-id", "uuid-from-_id"]
    assert post(client, jwt.encode({**no_sub, "role": "x"}, SECRET, algorithm="HS256")).status_code == 401


def test_dev_login_off_by_default(client, monkeypatch):
    monkeypatch.setattr(config, "DEV_LOGIN", False)
    assert client.post("/api/dev/login", json={"customer_id": "c1"}).status_code == 404
    assert client.get("/api/dev/customers").status_code == 404


def test_dev_login_token_is_accepted_by_normal_path(client, monkeypatch):
    monkeypatch.setattr(config, "DEV_LOGIN", True)
    tok = client.post("/api/dev/login", json={"customer_id": "c-42"}).json()["token"]
    assert post(client, tok).status_code == 200 and client.calls[0]["user_id"] == "c-42"


def test_dev_login_needs_secret(client, monkeypatch):
    monkeypatch.setattr(config, "DEV_LOGIN", True)
    monkeypatch.setattr(config, "JWT_SECRET", "")
    assert client.post("/api/dev/login", json={"customer_id": "c1"}).status_code == 400
