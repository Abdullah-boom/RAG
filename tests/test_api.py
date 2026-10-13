"""API contract and safety checks. The agent and Mongo are stubbed, so these run offline."""
import pytest
from fastapi.testclient import TestClient

from app import api

BRANCH = {"branch_id": "64a0000000000000000000aa", "name": "Gulberg", "city": "Lahore"}


@pytest.fixture
def client(monkeypatch):
    calls = []

    def fake_chat(message, *, thread_id, branch_id, user_id=None):
        calls.append({"message": message, "thread_id": thread_id, "branch_id": branch_id, "user_id": user_id})
        return {"answer": "Hi", "items": [], "suggestions": ["Show deals"], "cart": None, "handoff": False,
                "as_of": "2026-10-13T00:00:00Z", "debug": {"tools": ["search_menu"]}}

    monkeypatch.setattr(api, "chat", fake_chat)
    monkeypatch.setattr(api, "branches", lambda: [BRANCH])
    monkeypatch.setattr(api, "limiter", api.RateLimiter(3))
    monkeypatch.setattr(api.config, "API_DEBUG", False)
    c = TestClient(api.app)  # no `with`: skips lifespan (model + Redis warm-up)
    c.calls = calls
    return c


def test_chat_mints_session_and_hides_debug(client):
    r = client.post("/api/chat", json={"message": "hello", "branch_id": BRANCH["branch_id"]})
    assert r.status_code == 200
    body = r.json()
    assert len(body["session_id"]) == 32 and body["answer"] == "Hi" and "debug" not in body
    assert client.calls[0]["thread_id"] == f"{BRANCH['branch_id']}:{body['session_id']}"


def test_chat_keeps_valid_session_and_replaces_bad_one(client):
    sid = "a" * 32
    assert client.post("/api/chat", json={"message": "x", "branch_id": BRANCH["branch_id"],
                                          "session_id": sid}).json()["session_id"] == sid
    bad = client.post("/api/chat", json={"message": "x", "branch_id": BRANCH["branch_id"],
                                         "session_id": "../other-thread"}).json()["session_id"]
    assert bad != "../other-thread" and len(bad) == 32


def test_identity_cannot_be_sent(client):
    client.post("/api/chat", json={"message": "x", "branch_id": BRANCH["branch_id"], "user_id": "victim"})
    assert client.calls[0]["user_id"] is None


def test_unknown_branch_rejected(client):
    assert client.post("/api/chat", json={"message": "x", "branch_id": "nope"}).status_code == 404


def test_message_length_limit(client):
    r = client.post("/api/chat", json={"message": "x" * 1001, "branch_id": BRANCH["branch_id"]})
    assert r.status_code == 422 and not client.calls


def test_rate_limit(client):
    codes = [client.post("/api/chat", json={"message": "x", "branch_id": BRANCH["branch_id"]}).status_code
             for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_cors_only_for_configured_origins(client):
    ok = client.options("/api/chat", headers={"Origin": "http://localhost:5173",
                                              "Access-Control-Request-Method": "POST"})
    bad = client.options("/api/chat", headers={"Origin": "https://evil.example",
                                               "Access-Control-Request-Method": "POST"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-origin" not in bad.headers
