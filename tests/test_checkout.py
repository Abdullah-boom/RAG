"""Review -> explicit confirm -> hand-off to the order backend. The bot itself never places an order."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app import api, checkout, config
from app.agent.intents import prefetch_tools
from tests.test_branch_cart import A, B, GUEST_1, ITEMS, ROWS, add, guest
from tests.test_payments import CONFIG

USER = "cust-7"


@pytest.fixture
def client(monkeypatch):
    import app.cart as cart_mod
    monkeypatch.setattr(api, "branches", lambda: ROWS)
    monkeypatch.setattr(api, "limiter", api.RateLimiter(1000))
    monkeypatch.setattr(api, "payment_config", lambda b: CONFIG)
    monkeypatch.setattr(api, "user_from_request", lambda request: USER)
    monkeypatch.setattr(cart_mod.repo, "get_items",
                        lambda ids, b, detailed=False: [dict(ITEMS[i]) for i in ids if i in ITEMS])
    monkeypatch.setattr(api.repo, "get_branch_status",
                        lambda b: {"branch_id": b, "open_now": b != "c" * 24, "min_order_amount": 50})
    monkeypatch.setattr(config, "ORDER_BACKEND_URL", "")
    return TestClient(api.app)


def ready_cart(client, branch=A["branch_id"], method="COD"):
    add(client, branch)  # Bazinga Rs 540
    client.post("/api/cart/payment", json={"branch_id": branch, "method": method}, headers=guest(GUEST_1))


def review(client, branch=A["branch_id"]):
    return client.post("/api/checkout/review", json={"branch_id": branch}, headers=guest(GUEST_1)).json()


def confirm(client, token, branch=A["branch_id"]):
    return client.post("/api/checkout/confirm", json={"branch_id": branch, "confirm_token": token},
                       headers=guest(GUEST_1))


def backend(monkeypatch, reply):
    """Stand-in order backend; records what it was sent."""
    sent = []

    def post(url, json, headers, timeout):
        sent.append({"url": url, "json": json, "headers": headers})
        return reply if isinstance(reply, httpx.Response) else reply()

    monkeypatch.setattr(config, "ORDER_BACKEND_URL", "https://orders.example/api/chatbot-orders")
    monkeypatch.setattr(checkout.httpx, "post", post)
    return sent


# ---------- review ----------

def test_review_lists_everything_missing(client, monkeypatch):
    monkeypatch.setattr(api, "user_from_request", lambda request: None)
    codes = {p["code"] for p in review(client)["problems"]}
    assert codes == {"empty_cart", "no_payment_method", "login_required"}


def test_review_ready_returns_summary_and_token(client):
    ready_cart(client)
    r = review(client)
    assert r["ready"] and r["problems"] == [] and r["confirm_token"]
    assert r["summary"]["subtotal"] == 540 and r["summary"]["payment_method"]["total"] == 621  # 15% cash tax


def test_review_flags_minimum_order_and_closed_branch(client, monkeypatch):
    ready_cart(client)
    monkeypatch.setattr(api.repo, "get_branch_status", lambda b: {"open_now": False, "min_order_amount": 1000})
    assert {p["code"] for p in review(client)["problems"]} == {"branch_closed", "below_minimum_order"}


def test_review_flags_method_switched_off(client, monkeypatch):
    ready_cart(client, method="XPAY_CARD")
    monkeypatch.setattr(api, "payment_config", lambda b: {**CONFIG, "methods": CONFIG["methods"][:2]})
    assert review(client)["problems"] == [{"code": "payment_method_unavailable", "method": "XPAY_CARD"}]


# ---------- confirm ----------

def test_not_connected_keeps_cart_and_says_so(client):
    ready_cart(client)
    r = confirm(client, review(client)["confirm_token"]).json()
    assert r["status"] == "not_connected" and r["cart"]["lines"][0]["name"] == "Bazinga Burger"


def test_placed_hands_off_state_only_and_empties_that_cart(client, monkeypatch):
    sent = backend(monkeypatch, httpx.Response(201, json={"orderNo": "CHZ-1042", "status": "PLACED"}))
    ready_cart(client)
    ready_cart(client, branch=B["branch_id"])
    token = review(client)["confirm_token"]
    r = confirm(client, token).json()
    assert r["status"] == "placed" and r["order_no"] == "CHZ-1042" and r["cart"] is None
    body = sent[0]["json"]
    assert body["customer_id"] == USER and body["branch_id"] == A["branch_id"] and body["payment_method"] == "COD"
    assert body["items"] == [{"item_id": "p1", "name": "Bazinga Burger", "size": None, "qty": 1}]
    assert sent[0]["headers"]["Idempotency-Key"] == token
    # state only: these fields and nothing else (no prices to trust, no payment data)
    assert set(body) == {"source", "customer_id", "branch_id", "items", "payment_method", "estimate",
                         "idempotency_key"}
    # the other branch's cart is untouched
    assert client.get(f"/api/cart?branch_id={B['branch_id']}", headers=guest(GUEST_1)).json()["cart"]["item_count"] == 1


def test_cart_changed_after_review_is_refused(client, monkeypatch):
    sent = backend(monkeypatch, httpx.Response(201, json={"orderNo": "X"}))
    ready_cart(client)
    token = review(client)["confirm_token"]
    add(client, A["branch_id"])  # one more burger after seeing the summary
    r = confirm(client, token)
    assert r.status_code == 409 and r.json()["detail"] == "cart_changed" and sent == []
    client.post("/api/cart/payment", json={"branch_id": A["branch_id"], "method": "XPAY_CARD"}, headers=guest(GUEST_1))
    assert confirm(client, review(client)["confirm_token"]).json()["status"] == "placed"


def test_expired_or_forged_token(client):
    ready_cart(client)
    fp = checkout.fingerprint(f"user:{USER}", A["branch_id"], [{"item_id": "p1", "size": None, "qty": 1}], "COD")
    old = checkout.make_token(fp, now=0)
    assert confirm(client, old).json()["detail"] == "expired"
    assert confirm(client, "123.forged").json()["detail"] == "cart_changed"


def test_guest_cannot_confirm(client, monkeypatch):
    ready_cart(client)
    token = review(client)["confirm_token"]
    monkeypatch.setattr(api, "user_from_request", lambda request: None)
    assert confirm(client, token).status_code == 401


def test_backend_rejection_and_outage_keep_the_cart(client, monkeypatch):
    ready_cart(client)
    backend(monkeypatch, httpx.Response(422, json={"message": "Bazinga Burger just sold out"}))
    r = confirm(client, review(client)["confirm_token"]).json()
    assert r["status"] == "rejected" and r["message"] == "Bazinga Burger just sold out" and r["cart"]["item_count"] == 1

    def down():
        raise httpx.ConnectError("no route")

    backend(monkeypatch, down)
    r = confirm(client, review(client)["confirm_token"]).json()
    assert r["status"] == "failed" and r["cart"]["item_count"] == 1


# ---------- chat ----------

@pytest.mark.parametrize("message", ["order confirm karo", "place my order", "checkout", "order kar do bhai"])
def test_order_now_reviews_never_places(message):
    assert prefetch_tools(message) == [("review_order", {})]


def test_shopping_is_not_checkout():
    assert ("review_order", {}) not in prefetch_tools("I want to order a pizza")
