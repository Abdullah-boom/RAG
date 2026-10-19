"""API contract and safety checks. The agent and Mongo are stubbed, so these run offline."""
import pytest
from fastapi.testclient import TestClient

from app import api

BRANCH = {"branch_id": "64a0000000000000000000aa", "name": "Gulberg", "city": "Lahore"}
api._real_branches = api.branches  # the client fixture stubs branches(); DB-failure tests use the real one


@pytest.fixture
def client(monkeypatch):
    calls = []

    def fake_chat(message, *, thread_id, branch_id, user_id=None, cart=None, payment_method=None, **_):
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
    assert client.calls[0]["thread_id"] == f"{BRANCH['branch_id']}:guest:{body['session_id']}"


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


def test_db_blip_returns_503_not_crash(client, monkeypatch):
    from pymongo.errors import AutoReconnect

    def down():
        raise AutoReconnect("SSL handshake failed")

    monkeypatch.setattr(api, "branches", api.__dict__["_real_branches"])
    monkeypatch.setattr(api, "_branches", {"at": 0.0, "rows": []})
    monkeypatch.setattr(api.repo, "list_branches", down)
    monkeypatch.setattr(api.with_retry.__globals__["time"], "sleep", lambda s: None)
    r = client.post("/api/chat", json={"message": "x", "branch_id": BRANCH["branch_id"]})
    assert r.status_code == 503 and r.json()["detail"] == "database_unavailable" and not client.calls


def test_db_blip_serves_cached_branches(client, monkeypatch):
    from pymongo.errors import AutoReconnect

    def down():
        raise AutoReconnect("SSL handshake failed")

    monkeypatch.setattr(api, "branches", api.__dict__["_real_branches"])
    monkeypatch.setattr(api, "_branches", {"at": 0.0, "rows": [BRANCH]})  # stale cache
    monkeypatch.setattr(api.repo, "list_branches", down)
    monkeypatch.setattr(api.with_retry.__globals__["time"], "sleep", lambda s: None)
    r = client.post("/api/chat", json={"message": "x", "branch_id": BRANCH["branch_id"]})
    assert r.status_code == 200


def test_db_failed_turn_returns_503(client, monkeypatch):
    monkeypatch.setattr(api, "chat", lambda *a, **k: {"answer": "x", "error": "database_unavailable"})
    r = client.post("/api/chat", json={"message": "x", "branch_id": BRANCH["branch_id"]})
    assert r.status_code == 503 and r.json()["detail"] == "database_unavailable"


def parse_sse(text):
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        events.append((lines["event"], __import__("json").loads(lines["data"])))
    return events


def test_stream_sends_status_then_answer(client, monkeypatch):
    def fake_stream(message, **k):
        yield {"status": "Searching the menu…"}
        yield {"status": "Checking live prices…"}
        yield {"response": {"answer": "Rs 690", "items": [], "suggestions": [], "cart": None,
                            "handoff": False, "as_of": "t", "debug": {}}}

    monkeypatch.setattr(api, "chat_stream", fake_stream)
    r = client.post("/api/chat", json={"message": "x", "branch_id": BRANCH["branch_id"], "stream": True})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    assert [e for e, _ in events] == ["status", "status", "answer"]
    assert events[2][1]["answer"] == "Rs 690" and len(events[2][1]["session_id"]) == 32
    assert "debug" not in events[2][1]


def test_stream_db_failure_is_error_event(client, monkeypatch):
    monkeypatch.setattr(api, "chat_stream", lambda m, **k: iter([{"status": "Thinking…"},
                                                                 {"response": {"error": "database_unavailable"}}]))
    events = parse_sse(client.post("/api/chat", json={"message": "x", "branch_id": BRANCH["branch_id"],
                                                      "stream": True}).text)
    assert events[-1] == ("error", {"code": "database_unavailable"})


def test_stream_crash_is_error_event(client, monkeypatch):
    def boom(m, **k):
        yield {"status": "Thinking…"}
        raise RuntimeError("x")

    monkeypatch.setattr(api, "chat_stream", boom)
    events = parse_sse(client.post("/api/chat", json={"message": "x", "branch_id": BRANCH["branch_id"],
                                                      "stream": True}).text)
    assert events[-1] == ("error", {"code": "internal_error"})


# ---------- cart buttons: no LLM ----------

@pytest.fixture
def cart_client(client, monkeypatch):
    import app.cart as cart_mod
    from app.agent.graph import build_graph
    from langgraph.checkpoint.memory import InMemorySaver
    g = build_graph(InMemorySaver())
    monkeypatch.setattr(api, "graph", lambda: g)
    item = {"item_id": "p1", "name": "Black Pepper Tikka", "available": True, "price": None, "price_from": 690,
            "sizes": [{"size": "Small", "price": 690}, {"size": "Regular", "price": 1350}]}
    monkeypatch.setattr(cart_mod.repo, "get_items", lambda ids, b, detailed=False: [dict(item) for i in ids if i == "p1"])
    client.graph = g
    return client


def test_cart_add_without_size_adds_nothing(cart_client):
    r = cart_client.post("/api/cart/add", json={"branch_id": BRANCH["branch_id"], "item_id": "p1"}).json()
    assert not r["added"] and r["problems"][0]["error"] == "size_required" and r["cart"] is None
