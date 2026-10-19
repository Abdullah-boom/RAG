"""Payment method choice: per-branch availability, tax by method, chat tools, and card data kept out of chat."""
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

import app.agent.graph as g
from app import api, payments, tools
from tests.test_branch_cart import A, B, GUEST_1, ITEMS, ROWS, add, guest

CONFIG = {"methods": [{"label": "COD", "payment_type": "COD", "available": True},
                      {"label": "COP", "payment_type": "COP", "available": True},
                      {"label": "XPAY_CARD", "payment_type": "ONLINE", "available": True},
                      {"label": "CARD_MACHINE", "payment_type": "ONLINE", "available": True},
                      {"label": "XPAY_JAZZCASH", "payment_type": "ONLINE", "available": False},
                      {"label": "SOMETHING_NEW", "payment_type": "ONLINE", "available": True}],
          "cash_tax_rate": 15, "online_tax_rate": 10, "tax_exclusive": True}


# ---------- rules ----------

def test_only_available_known_methods_cheapest_first():
    opts = payments.options(CONFIG, 1000)
    codes = [o["code"] for o in opts]
    assert "XPAY_JAZZCASH" not in codes and "SOMETHING_NEW" not in codes  # off / can't describe it
    assert codes[:2] == ["CARD_MACHINE", "XPAY_CARD"] and set(codes[2:]) == {"COD", "COP"}
    card = next(o for o in opts if o["code"] == "XPAY_CARD")
    cod = next(o for o in opts if o["code"] == "COD")
    assert (card["tax"], card["total"], card["pay_now"]) == (100, 1100, True)
    assert (cod["tax"], cod["total"], cod["pay_now"]) == (150, 1150, False)


def test_inclusive_tax_is_inside_the_prices():
    assert payments.tax_estimate(1160, 16, exclusive=False) == {"tax_rate": 16, "tax": 160, "total": 1160}


@pytest.mark.parametrize("said,code", [("COD", "COD"), ("cash on delivery", "COD"), ("Cash on pickup", "COP"),
                                       ("online card", "XPAY_CARD"), ("card machine", "CARD_MACHINE"),
                                       ("card", None), ("jazzcash online", None), ("bitcoin", None)])
def test_find_method_by_code_or_words(said, code):
    found = payments.find(CONFIG, said)
    assert (found["code"] if found else None) == code  # "card" is ambiguous; JazzCash is switched off


@pytest.mark.parametrize("text,is_card", [("4111 1111 1111 1111", True), ("my card is 4111-1111-1111-1111", True),
                                         ("5500005555555559 exp 12/27", True), ("call me 0300 1234567", False),
                                         ("order 1234567890123", False), ("2 pizzas for 2500", False)])
def test_card_number_detection(text, is_card):
    assert payments.find_card_number(text) is is_card


# ---------- chat tools ----------

def test_set_payment_tool_validates_against_branch(monkeypatch):
    monkeypatch.setattr(tools.repo, "get_payment_config", lambda b: CONFIG)
    ctx = tools.RequestContext(None, "b1")
    ok, lines = tools.set_payment_method(ctx, tools.PaymentArgs(method="cash on delivery"), [])
    assert ok["payment_method_selected"] == "COD" and ok["pay_now"] is False and lines == []
    bad, _ = tools.set_payment_method(ctx, tools.PaymentArgs(method="jazzcash online"), [])
    assert bad["error"] == "unknown_or_unavailable_method" and "COD" in [m["code"] for m in bad["available"]]
    which, _ = tools.set_payment_method(ctx, tools.PaymentArgs(method="card karunga"), [])
    assert which["error"] == "ambiguous_method" and {m["code"] for m in which["ask_which"]} == {"XPAY_CARD", "CARD_MACHINE"}


def test_card_number_never_reaches_model_or_memory(monkeypatch):
    monkeypatch.setattr(g, "_invoke_llm", lambda *a, **k: pytest.fail("a card number must not reach the model"))
    monkeypatch.setattr(g, "price_cart", lambda lines, b: {"lines": [], "item_count": 0, "subtotal": 0})
    monkeypatch.setattr(g, "_trace", lambda r: None)
    graph = g.build_graph(InMemorySaver())
    cfg = {"configurable": {"thread_id": "t", "user_id": None, "branch_id": "b1"}}
    out = graph.invoke({"messages": [g.HumanMessage(content="mera card 4111 1111 1111 1111 hai, pay kar do")],
                        "cart": []}, config=cfg)
    assert out["response"]["answer"] == g.CARD_WARNINGS["roman_urdu"]
    stored = " ".join(m.content for m in graph.get_state(cfg).values["messages"])
    assert "4111" not in stored and "[card number removed]" in stored


def test_chat_choice_is_reported_back(monkeypatch):
    monkeypatch.setattr(tools.repo, "get_payment_config", lambda b: CONFIG)
    monkeypatch.setattr(g.repo, "get_branch_status", lambda b: {"branch": "I-8", "city": "Islamabad"})
    monkeypatch.setattr(g, "price_cart", lambda lines, b: {"lines": [], "item_count": 0, "subtotal": 0})
    monkeypatch.setattr(g, "_trace", lambda r: None)
    replies = iter([AIMessage(content="", tool_calls=[{"name": "set_payment_method", "args": {"method": "COD"},
                                                       "id": "c1"}]),
                    AIMessage(content="Done, you'll pay cash on delivery.")])
    monkeypatch.setattr(g, "_invoke_llm", lambda msgs, allow_tools: next(replies))
    graph = g.build_graph(InMemorySaver())
    cfg = {"configurable": {"thread_id": "t", "user_id": None, "branch_id": "b1"}}
    out = graph.invoke({"messages": [g.HumanMessage(content="I'll go with that")], "cart": []}, config=cfg)
    assert out["response"]["payment_method_set"] == "COD"


# ---------- API ----------

@pytest.fixture
def client(monkeypatch):
    import app.cart as cart_mod
    seen = []

    def fake_chat(message, *, thread_id, branch_id, user_id=None, cart=None, payment_method=None, **_):
        seen.append(payment_method)
        out = {"answer": "ok", "items": [], "suggestions": [], "cart": None, "handoff": False, "as_of": "t",
               "cart_lines": list(cart or [])}
        if message == "cash please":
            out["payment_method_set"] = "COD"
        return out

    monkeypatch.setattr(api, "chat", fake_chat)
    monkeypatch.setattr(api, "branches", lambda: ROWS)
    monkeypatch.setattr(api, "limiter", api.RateLimiter(1000))
    monkeypatch.setattr(api, "payment_config", lambda b: CONFIG)
    monkeypatch.setattr(cart_mod.repo, "get_items",
                        lambda ids, b, detailed=False: [dict(ITEMS[i]) for i in ids if i in ITEMS])
    c = TestClient(api.app)
    c.seen = seen
    return c


def test_list_methods_with_estimates_for_the_cart(client):
    add(client, A["branch_id"])  # Bazinga Rs 540
    r = client.get(f"/api/payment-methods?branch_id={A['branch_id']}", headers=guest(GUEST_1)).json()
    card = next(m for m in r["methods"] if m["code"] == "XPAY_CARD")
    assert r["cart_subtotal"] == 540 and card["total"] == 594 and r["chosen"] is None and "secure" in r["secure_note"]


def test_choose_method_is_saved_per_branch_cart(client):
    add(client, A["branch_id"])
    add(client, B["branch_id"])
    r = client.post("/api/cart/payment", json={"branch_id": A["branch_id"], "method": "COD"},
                    headers=guest(GUEST_1)).json()
    assert r["cart"]["checkout"]["payment_method"]["code"] == "COD" and r["cart"]["checkout"]["payment_method"]["total"] == 621
    a = client.get(f"/api/cart?branch_id={A['branch_id']}", headers=guest(GUEST_1)).json()["cart"]
    b = client.get(f"/api/cart?branch_id={B['branch_id']}", headers=guest(GUEST_1)).json()["cart"]
    assert a["checkout"]["payment_method"]["code"] == "COD" and b["checkout"]["payment_method"] is None


def test_unavailable_method_is_refused(client):
    for method in ("XPAY_JAZZCASH", "SOMETHING_NEW", "VISA"):
        r = client.post("/api/cart/payment", json={"branch_id": A["branch_id"], "method": method}, headers=guest(GUEST_1))
        assert r.status_code == 409 and r.json()["detail"] == "payment_method_unavailable"


def test_method_switched_off_later_disappears_from_cart(client, monkeypatch):
    add(client, A["branch_id"])
    client.post("/api/cart/payment", json={"branch_id": A["branch_id"], "method": "XPAY_CARD"}, headers=guest(GUEST_1))
    off = {**CONFIG, "methods": [m for m in CONFIG["methods"] if m["label"] != "XPAY_CARD"]}
    monkeypatch.setattr(api, "payment_config", lambda b: off)
    cart = client.get(f"/api/cart?branch_id={A['branch_id']}", headers=guest(GUEST_1)).json()["cart"]
    assert cart["checkout"]["payment_method"] is None  # never show a choice the branch no longer accepts


def test_chat_gets_and_saves_the_payment_choice(client):
    add(client, A["branch_id"])
    r = client.post("/api/chat", json={"message": "cash please", "branch_id": A["branch_id"]}, headers=guest(GUEST_1))
    assert client.seen[-1] is None
    client.post("/api/chat", json={"message": "hi", "branch_id": A["branch_id"]}, headers=guest(GUEST_1))
    assert client.seen[-1] == "COD"
    assert r.json()["guest_id"] == GUEST_1


def test_payment_answer_is_not_a_branch_or_order_claim():
    from app.agent.guards import check_answer
    answer = ("Cash on delivery (COD) is available for delivery. You pay when the order is delivered. "
              "The tax rate for this method is 16%.")
    result = [{"methods": [{"code": "COD", "name": "Cash on delivery", "tax_rate": 16}]}]
    assert check_answer(answer, result, ["get_payment_methods"], "is cash on delivery available?", []) == []
    # without a payment tool, an order-status claim is still caught
    assert check_answer("Your order is delivered.", [], [], "where is my order", []) != []
