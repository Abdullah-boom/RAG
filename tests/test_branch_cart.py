"""Location -> default branch, server-checked branch switching, and one independent cart per branch.
Mongo, the LLM and Redis are stubbed, so these run offline."""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import api, cart_store, locator
from app.data import repositories as repo

DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
ALL_DAY = [{"day": d, "timing": [{"startTime": "12:00am", "endTime": "11:59pm"},
                                 {"startTime": "11:59pm", "endTime": "12:00am"}]} for d in DAYS]


def branch(bid, name, lng, lat, accepting=True, timing=ALL_DAY):
    return {"branch_id": bid, "name": name, "city": "Lahore", "accepting_orders": accepting,
            "timing": timing, "location": [lng, lat]}


A = branch("a" * 24, "Gulberg", 74.34, 31.51)
B = branch("b" * 24, "Johar Town", 74.27, 31.46)
C = branch("c" * 24, "Model Town", 74.32, 31.48, accepting=False)  # closed (e.g. heavy rain)
ROWS = [A, B, C]
ITEMS = {"p1": {"item_id": "p1", "name": "Bazinga Burger", "available": True, "price": 540},
         "p2": {"item_id": "p2", "name": "Crown Crust", "available": True, "price": None,
                "sizes": [{"size": "Regular", "price": 1480}, {"size": "Large", "price": 1960}]}}
GUEST_1, GUEST_2 = "1" * 32, "2" * 32


# ---------- point in polygon ----------

def test_point_query_is_lng_lat_for_geojson():
    q = repo.point_query(lat=31.51, lng=74.34)
    assert q["polygon"]["$geoIntersects"]["$geometry"] == {"type": "Point", "coordinates": [74.34, 31.51]}
    assert q["isActive"] is True and q["isEnabled"] is True  # disabled / inactive areas never match


class FakePolygons:
    def __init__(self, rows):
        self.rows, self.queries = rows, []

    def find(self, q, projection):
        self.queries.append(q)
        rows = self.rows

        class Cursor(list):
            def max_time_ms(self, ms):
                return self
        return Cursor(rows)


def test_red_zone_removes_branch_and_duplicates_collapse(monkeypatch):
    from bson import ObjectId
    a, b = ObjectId("a" * 24), ObjectId("b" * 24)
    polys = FakePolygons([{"branchId": a, "type": "DELIVERYAREA"}, {"branchId": a, "type": "DELIVERYAREA"},
                          {"branchId": b, "type": "DELIVERYAREA"}, {"branchId": b, "type": "REDZONE"}])
    monkeypatch.setattr(repo, "product_db", lambda: type("DB", (), {"polygons": polys})())
    assert repo.branches_covering(31.5, 74.3) == ["a" * 24]
    assert polys.queries[0]["type"] == {"$in": ["DELIVERYAREA", "REDZONE"]}


# ---------- choosing the default branch ----------

def test_single_open_branch_matches():
    r = locator.choose_branch(31.51, 74.34, [A["branch_id"]], ROWS)
    assert r == {"reason": locator.MATCHED, "branch_id": A["branch_id"], "covering": [A["branch_id"]]}


def test_outside_every_polygon_assigns_nothing():
    assert locator.choose_branch(24.8, 66.97, [], ROWS) == {"reason": locator.NO_COVERAGE, "branch_id": None,
                                                           "covering": []}


def test_polygon_of_unlisted_branch_is_ignored():
    """An inactive / hidden / test branch's delivery area must not pick a branch the customer can't use."""
    r = locator.choose_branch(31.5, 74.3, ["f" * 24], ROWS)
    assert r["reason"] == locator.NO_COVERAGE and r["branch_id"] is None


def test_only_closed_branch_covers_point():
    r = locator.choose_branch(31.48, 74.32, [C["branch_id"]], ROWS)
    assert r == {"reason": locator.CLOSED, "branch_id": None, "covering": [C["branch_id"]]}


def test_overlap_prefers_open_then_nearest():
    # Point next to Johar Town; Model Town (closed) also covers it.
    r = locator.choose_branch(31.461, 74.271, [A["branch_id"], C["branch_id"], B["branch_id"]], ROWS)
    assert r["branch_id"] == B["branch_id"] and r["covering"] == [B["branch_id"], A["branch_id"], C["branch_id"]]


def test_hours_that_cross_midnight():
    timing = [{"day": "MONDAY", "timing": [{"startTime": "10:00am", "endTime": "02:00am"}]}]
    monday_late = datetime(2026, 10, 12, 1, 30, tzinfo=repo.PK_TZ)
    monday_morning = datetime(2026, 10, 12, 9, 0, tzinfo=repo.PK_TZ)
    assert repo.open_now(branch("x" * 24, "X", 0, 0, timing=timing), monday_late)
    assert not repo.open_now(branch("x" * 24, "X", 0, 0, timing=timing), monday_morning)


# ---------- API ----------

@pytest.fixture
def client(monkeypatch):
    import app.cart as cart_mod
    calls = []

    def fake_chat(message, *, thread_id, branch_id, user_id=None, cart=None, payment_method=None):
        """Stands in for the agent: 'add burger' adds p1 to whatever cart it was given."""
        calls.append({"branch_id": branch_id, "cart_in": list(cart or []), "thread_id": thread_id})
        lines = list(cart or [])
        if message == "add burger":
            lines.append({"item_id": "p1", "name": "Bazinga Burger", "size": None, "qty": 1})
        return {"answer": "ok", "items": [], "suggestions": [], "cart": None, "handoff": False, "as_of": "t",
                "cart_lines": lines}

    monkeypatch.setattr(api, "chat", fake_chat)
    monkeypatch.setattr(api, "branches", lambda: ROWS)
    monkeypatch.setattr(api, "limiter", api.RateLimiter(1000))
    monkeypatch.setattr(cart_mod.repo, "get_items",
                        lambda ids, b, detailed=False: [dict(ITEMS[i]) for i in ids if i in ITEMS])

    def status(bid):
        row = next(r for r in ROWS if r["branch_id"] == bid)
        return {"branch_id": bid, "branch": row["name"], "open_now": repo.open_now(row)}

    monkeypatch.setattr(api.repo, "get_branch_status", status)
    c = TestClient(api.app)
    c.calls = calls
    return c


def guest(gid):
    return {api.GUEST_HEADER: gid}


def add(client, branch_id, item="p1", headers=None, **extra):
    return client.post("/api/cart/add", json={"branch_id": branch_id, "item_id": item, **extra},
                       headers=headers or guest(GUEST_1))


def cart_names(client, branch_id, headers=None):
    cart = client.get(f"/api/cart?branch_id={branch_id}", headers=headers or guest(GUEST_1)).json()["cart"]
    return [(l["name"], l["size"], l["qty"]) for l in cart["lines"]] if cart else []


def test_branch_list_says_which_are_open(client):
    rows = {b["branch_id"]: b for b in client.get("/api/branches").json()["branches"]}
    assert rows[A["branch_id"]]["open_now"] and not rows[C["branch_id"]]["open_now"]
    assert set(rows[A["branch_id"]]) == {"branch_id", "name", "city", "open_now", "accepting_orders"}


def test_locate_matches_branch(client, monkeypatch):
    seen = []
    monkeypatch.setattr(api.repo, "branches_covering", lambda lat, lng: seen.append((lat, lng)) or [B["branch_id"]])
    r = client.post("/api/branches/locate", json={"lat": 31.46, "lng": 74.27}).json()
    assert r["reason"] == "matched" and r["branch"]["branch_id"] == B["branch_id"] and seen == [(31.46, 74.27)]


def test_locate_outside_coverage_and_closed(client, monkeypatch):
    monkeypatch.setattr(api.repo, "branches_covering", lambda lat, lng: [])
    assert client.post("/api/branches/locate", json={"lat": 24.8, "lng": 66.9}).json() == {
        "reason": "no_coverage", "branch": None, "covering": []}
    monkeypatch.setattr(api.repo, "branches_covering", lambda lat, lng: [C["branch_id"]])
    r = client.post("/api/branches/locate", json={"lat": 31.48, "lng": 74.32}).json()
    assert r["reason"] == "branch_closed" and r["branch"] is None and r["covering"][0]["open_now"] is False


def test_locate_rejects_impossible_coordinates(client):
    assert client.post("/api/branches/locate", json={"lat": 120, "lng": 74}).status_code == 422
    assert client.post("/api/branches/locate", json={"lat": 31.5}).status_code == 422


def test_locate_db_failure_is_503(client, monkeypatch):
    from pymongo.errors import AutoReconnect

    def down(lat, lng):
        raise AutoReconnect("x")

    monkeypatch.setattr(api.repo, "branches_covering", down)
    monkeypatch.setattr(api.with_retry.__globals__["time"], "sleep", lambda s: None)
    r = client.post("/api/branches/locate", json={"lat": 31.5, "lng": 74.3})
    assert r.status_code == 503 and r.json()["detail"] == "database_unavailable"


def test_select_open_branch_returns_its_cart(client):
    add(client, A["branch_id"])
    r = client.post("/api/branches/select", json={"branch_id": A["branch_id"]}, headers=guest(GUEST_1)).json()
    assert r["branch"]["branch_id"] == A["branch_id"] and r["cart"]["lines"][0]["name"] == "Bazinga Burger"
    r = client.post("/api/branches/select", json={"branch_id": B["branch_id"]}, headers=guest(GUEST_1)).json()
    assert r["cart"] is None  # B has no cart yet: empty, and A's is untouched
    assert cart_names(client, A["branch_id"]) == [("Bazinga Burger", None, 1)]


def test_select_closed_or_unknown_branch_is_refused(client):
    assert client.post("/api/branches/select", json={"branch_id": C["branch_id"]}).status_code == 409
    assert client.post("/api/branches/select", json={"branch_id": "nope"}).status_code == 404


def test_each_branch_has_its_own_cart(client):
    add(client, A["branch_id"])
    add(client, B["branch_id"], item="p2", size="Large")
    add(client, A["branch_id"])
    assert cart_names(client, A["branch_id"]) == [("Bazinga Burger", None, 2)]
    assert cart_names(client, B["branch_id"]) == [("Crown Crust", "Large", 1)]


def test_cart_operations_touch_only_their_branch(client):
    add(client, A["branch_id"])
    add(client, B["branch_id"])
    h = guest(GUEST_1)
    client.post("/api/cart/quantity", json={"branch_id": B["branch_id"], "line_no": 1, "qty": 5}, headers=h)
    assert cart_names(client, B["branch_id"]) == [("Bazinga Burger", None, 5)]
    client.post("/api/cart/remove", json={"branch_id": B["branch_id"], "line_no": 1}, headers=h)
    assert cart_names(client, B["branch_id"]) == []
    add(client, B["branch_id"])
    client.post("/api/cart/clear", json={"branch_id": B["branch_id"]}, headers=h)
    assert cart_names(client, B["branch_id"]) == [] and cart_names(client, A["branch_id"]) == [("Bazinga Burger", None, 1)]


def test_quantity_for_missing_line_changes_nothing(client):
    add(client, A["branch_id"])
    r = client.post("/api/cart/quantity", json={"branch_id": A["branch_id"], "line_no": 3, "qty": 2},
                    headers=guest(GUEST_1)).json()
    assert r["error"] == "not_in_cart" and cart_names(client, A["branch_id"]) == [("Bazinga Burger", None, 1)]


def test_cart_for_unknown_branch_is_refused(client):
    assert add(client, "f" * 24).status_code == 404


def test_guest_id_is_minted_and_carts_are_per_owner(client):
    r = client.post("/api/cart/add", json={"branch_id": A["branch_id"], "item_id": "p1"}).json()
    minted = r["guest_id"]
    assert len(minted) == 32 and cart_names(client, A["branch_id"], headers=guest(minted)) == [("Bazinga Burger", None, 1)]
    assert cart_names(client, A["branch_id"], headers=guest(GUEST_2)) == []  # another guest sees nothing
    assert client.get(f"/api/cart?branch_id={A['branch_id']}").json() == {"guest_id": None, "cart": None}


def test_logged_in_cart_belongs_to_the_account(client, monkeypatch):
    monkeypatch.setattr(api, "user_from_request", lambda request: "cust-7")
    add(client, A["branch_id"], headers=guest(GUEST_1))
    monkeypatch.setattr(api, "user_from_request", lambda request: None)
    assert cart_names(client, A["branch_id"], headers=guest(GUEST_1)) == []  # the guest cart is separate
    monkeypatch.setattr(api, "user_from_request", lambda request: "cust-7")
    assert cart_names(client, A["branch_id"], headers=guest(GUEST_2)) == [("Bazinga Burger", None, 1)]


def test_chat_uses_and_saves_the_branch_cart(client):
    add(client, A["branch_id"], item="p2", size="Regular")
    r = client.post("/api/chat", json={"message": "add burger", "branch_id": A["branch_id"]},
                    headers=guest(GUEST_1)).json()
    assert client.calls[-1]["cart_in"][0]["item_id"] == "p2" and r["guest_id"] == GUEST_1
    assert cart_names(client, A["branch_id"]) == [("Crown Crust", "Regular", 1), ("Bazinga Burger", None, 1)]
    assert cart_names(client, B["branch_id"]) == []


def test_cart_survives_new_chat_and_branch_round_trip(client):
    h = guest(GUEST_1)
    client.post("/api/chat", json={"message": "add burger", "branch_id": A["branch_id"]}, headers=h)
    client.post("/api/chat", json={"message": "hi", "branch_id": B["branch_id"]}, headers=h)  # switch away
    # back on A with a brand-new conversation (no session_id): the cart is still there
    client.post("/api/chat", json={"message": "hi", "branch_id": A["branch_id"]}, headers=h)
    assert client.calls[-1]["cart_in"] == [{"item_id": "p1", "name": "Bazinga Burger", "size": None, "qty": 1}]


def test_failed_chat_turn_does_not_save_cart(client, monkeypatch):
    add(client, A["branch_id"])
    monkeypatch.setattr(api, "chat", lambda *a, **k: {"error": "database_unavailable", "cart_lines": []})
    r = client.post("/api/chat", json={"message": "x", "branch_id": A["branch_id"]}, headers=guest(GUEST_1))
    assert r.status_code == 503 and cart_names(client, A["branch_id"]) == [("Bazinga Burger", None, 1)]


def test_busy_cart_is_409_not_lost_update(client, fresh_cart_store, monkeypatch):
    monkeypatch.setattr(cart_store, "LOCK_WAIT_S", 0.05)
    owner = cart_store.owner_key(None, GUEST_1)
    with fresh_cart_store.lock(owner, A["branch_id"]):  # e.g. a chat turn still running
        r = add(client, A["branch_id"])
    assert r.status_code == 409 and r.json()["detail"] == "cart_busy"
    assert add(client, B["branch_id"]).status_code == 200  # other branches aren't blocked


def test_cart_store_down_is_503(client, monkeypatch):
    class Down(cart_store.MemoryCartStore):
        def get(self, owner, branch_id):
            raise cart_store.CartUnavailable()

    monkeypatch.setattr(api, "cart_store", lambda: Down())
    r = client.get(f"/api/cart?branch_id={A['branch_id']}", headers=guest(GUEST_1))
    assert r.status_code == 503 and r.json()["detail"] == "cart_unavailable"
