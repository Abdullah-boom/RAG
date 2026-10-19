"""Delivery / pickup, payment that fits the order type, and placing the order with a plain "yes" in chat.
Mongo, the LLM, Redis and the order backend are stubbed, so these run offline."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app import api, checkout, config, order_flow, payments, tools
from app.agent import understand
from app.agent.intents import order_type_call
from tests.test_branch_cart import A, B, C, ITEMS, ROWS
from tests.test_payments import CONFIG

USER = "cust-9"
OWNER = f"user:{USER}"
SESSION = "5" * 32
SPOT = {"lat": 31.51, "lng": 74.34}


@pytest.fixture
def client(monkeypatch):
    import app.cart as cart_mod
    script = {}  # message -> extra response fields the stand-in agent returns
    calls = []

    def fake_chat(message, *, thread_id, branch_id, user_id=None, cart=None, payment_method=None, order=None,
                  tracked=None):
        calls.append({"message": message, "branch_id": branch_id, "order": order, "payment": payment_method,
                      "tracked": tracked})
        return {"answer": "ok", "items": [], "suggestions": [], "cart": None, "handoff": False, "as_of": "t",
                "cart_lines": list(cart or []), **script.get(message, {})}

    monkeypatch.setattr(api, "chat", fake_chat)
    monkeypatch.setattr(api, "branches", lambda: ROWS)
    monkeypatch.setattr(api, "limiter", api.RateLimiter(1000))
    monkeypatch.setattr(api, "payment_config", lambda b: CONFIG)
    monkeypatch.setattr(api, "user_from_request", lambda request: USER)
    monkeypatch.setattr(cart_mod.repo, "get_items",
                        lambda ids, b, detailed=False: [dict(ITEMS[i]) for i in ids if i in ITEMS])
    # A: open, has pickup. B: open, delivery only. C: closed.
    monkeypatch.setattr(api.repo, "get_branch_status", lambda b: {
        "branch_id": b, "branch": {A["branch_id"]: "Gulberg", B["branch_id"]: "Johar Town"}.get(b, "Model Town"),
        "open_now": b != C["branch_id"], "pickup": b != B["branch_id"], "min_order_amount": 50})
    monkeypatch.setattr(api.repo, "branches_covering", lambda lat, lng: [A["branch_id"]] if lat > 31.5 else [])
    monkeypatch.setattr(config, "ORDER_BACKEND_URL", "")
    c = TestClient(api.app)
    c.script, c.calls = script, calls
    return c


def set_type(client, kind, **body):
    return client.post("/api/order/type", json={"order_type": kind, **body})


def add(client, branch_id=A["branch_id"]):
    return client.post("/api/cart/add", json={"branch_id": branch_id, "item_id": "p1"})


def say(client, message, branch_id=A["branch_id"], session=SESSION):
    return client.post("/api/chat", json={"message": message, "branch_id": branch_id, "session_id": session})


def backend(monkeypatch, reply):
    sent = []

    def post(url, json, headers, timeout):
        sent.append(json)
        return reply

    monkeypatch.setattr(config, "ORDER_BACKEND_URL", "https://orders.example/api/chatbot-orders")
    monkeypatch.setattr(checkout.httpx, "post", post)
    return sent


# ---------- payment methods fit the order type ----------

def test_methods_depend_on_order_type():
    codes = lambda kind: {o["code"] for o in payments.options(CONFIG, order_type=kind)}
    assert codes("DELIVERY") == {"COD", "XPAY_CARD", "CARD_MACHINE"}
    assert codes("PICKUP") == {"COP", "XPAY_CARD"}
    assert codes(None) == {"COD", "COP", "XPAY_CARD", "CARD_MACHINE"}  # not chosen yet: everything
    assert payments.find(CONFIG, "cash", "PICKUP")["code"] == "COP"  # "cash" is no longer ambiguous


# ---------- choosing delivery or pickup ----------

def test_delivery_finds_the_branch_from_the_location(client):
    r = set_type(client, "DELIVERY", **SPOT).json()
    assert r["branch"]["branch_id"] == A["branch_id"]
    assert r["order"] == {"type": "DELIVERY", "type_name": "Delivery", "payment_method": None}
    assert "lat" not in r["order"]  # the spot is kept for the order, not echoed back
    assert api.cart_store().get_order(r["guest_id"], A["branch_id"]) is None  # guest owner key is prefixed
    assert api.cart_store().get_order(OWNER, A["branch_id"]) == {"type": "DELIVERY", **SPOT}


def test_delivery_outside_every_area_or_without_location(client):
    r = set_type(client, "DELIVERY", lat=31.40, lng=74.30)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_coverage"
    assert set_type(client, "DELIVERY").status_code == 422


def test_pickup_needs_an_open_branch_that_offers_it(client):
    assert set_type(client, "PICKUP", branch_id=A["branch_id"]).json()["order"]["type"] == "PICKUP"
    assert set_type(client, "PICKUP", branch_id=B["branch_id"]).json()["detail"]["code"] == "pickup_not_offered"
    assert set_type(client, "PICKUP", branch_id=C["branch_id"]).json()["detail"]["code"] == "branch_closed"


def test_switching_type_clears_a_payment_that_no_longer_fits(client):
    set_type(client, "DELIVERY", **SPOT)
    client.post("/api/cart/payment", json={"branch_id": A["branch_id"], "method": "COD"})
    r = set_type(client, "PICKUP", branch_id=A["branch_id"]).json()
    assert r["order"]["payment_method"] is None  # no cash on delivery for a pickup
    client.post("/api/cart/payment", json={"branch_id": A["branch_id"], "method": "XPAY_CARD"})
    assert set_type(client, "DELIVERY", **SPOT).json()["order"]["payment_method"]["code"] == "XPAY_CARD"


def test_cart_follows_the_customer_to_the_new_branch(client):
    set_type(client, "DELIVERY", **SPOT)
    add(client)
    r = set_type(client, "PICKUP", branch_id=A["branch_id"], from_branch_id=A["branch_id"]).json()
    assert r["cart"]["item_count"] == 1 and not r["cart_moved"]  # same branch: nothing to move
    api.cart_store().set(OWNER, B["branch_id"], [{"item_id": "p1", "name": "Bazinga Burger", "size": None, "qty": 2}])
    api.cart_store().set(OWNER, A["branch_id"], [])
    r = set_type(client, "PICKUP", branch_id=A["branch_id"], from_branch_id=B["branch_id"]).json()
    assert r["cart_moved"] and r["cart"]["item_count"] == 2
    assert api.cart_store().get(OWNER, B["branch_id"]) == []


def test_payment_endpoint_refuses_a_method_for_the_other_type(client):
    set_type(client, "PICKUP", branch_id=A["branch_id"])
    r = client.post("/api/cart/payment", json={"branch_id": A["branch_id"], "method": "COD"})
    assert r.status_code == 409 and r.json()["detail"] == "payment_method_wrong_type"
    methods = client.get(f"/api/payment-methods?branch_id={A['branch_id']}").json()
    assert methods["order_type"] == "PICKUP" and {m["code"] for m in methods["methods"]} == {"COP", "XPAY_CARD"}


# ---------- switching in chat ----------

@pytest.mark.parametrize("message,kind", [("pickup kar do", "pickup"), ("make it delivery", "delivery"),
                                          ("delivery se pickup kar do", "pickup"),
                                          ("I want pickup instead of delivery", "pickup"),
                                          ("main khud le lunga", "pickup")])
def test_switch_words(message, kind):
    assert order_type_call(message) == ("set_order_type", {"order_type": kind})


@pytest.mark.parametrize("message", ["do you deliver to G-11?", "cash on delivery karunga", "is pickup available?"])
def test_not_a_switch(message):
    assert order_type_call(message) is None


def test_plan_routes_the_switch_by_the_customers_words():
    plan = understand.Plan(intent="order_type", order_type="delivery", language="roman_urdu")
    calls, topics = understand.route(plan, "delivery se pickup kar do")
    assert calls == [("set_order_type", {"order_type": "pickup"})] and "checkout" in topics


def test_chat_switch_is_applied_and_shown(client):
    set_type(client, "DELIVERY", **SPOT)
    client.post("/api/cart/payment", json={"branch_id": A["branch_id"], "method": "COD"})
    client.script["pickup kar do"] = {"order_type_set": {"order_type_selected": "PICKUP", "needs": None}}
    r = say(client, "pickup kar do").json()
    assert r["order"] == {"type": "PICKUP", "type_name": "Pickup", "payment_method": None} and r["action"] is None
    assert say(client, "hi").json()["order"]["type"] == "PICKUP"
    assert client.calls[-1]["order"] == {"type": "PICKUP"}


def test_chat_switch_that_needs_the_app_comes_back_as_an_action(client):
    set_type(client, "PICKUP", branch_id=A["branch_id"])
    client.script["delivery chahiye"] = {"order_type_set": {"order_type_selected": "DELIVERY", "needs": "location"}}
    r = say(client, "delivery chahiye").json()
    assert r["action"] == {"type": "need_location", "order_type": "DELIVERY"} and r["order"]["type"] == "PICKUP"


def test_switch_tool_works_out_what_is_needed(monkeypatch):
    monkeypatch.setattr(tools.repo, "get_branch_status", lambda b: {"pickup": False})
    monkeypatch.setattr(tools.repo, "branches_covering", lambda lat, lng: ["x"])
    ctx = lambda order, pay=None: tools.RequestContext(USER, "x", payment_method=pay, order=order)
    out, _ = tools.set_order_type(ctx({"type": "DELIVERY", **SPOT}, "COD"), tools.OrderTypeArgs(order_type="pickup"), [])
    assert out["needs"] == "pickup_branch" and out["payment_cleared"] == "Cash on delivery"
    out, _ = tools.set_order_type(ctx({"type": "PICKUP"}), tools.OrderTypeArgs(order_type="delivery"), [])
    assert out["needs"] == "location"  # no spot known yet
    out, _ = tools.set_order_type(ctx({"type": "PICKUP", **SPOT}), tools.OrderTypeArgs(order_type="home delivery"), [])
    assert out["order_type_selected"] == "DELIVERY" and "needs" not in out


# ---------- review in chat, then "yes" ----------

def ready(client):
    set_type(client, "DELIVERY", **SPOT)
    add(client)
    client.post("/api/cart/payment", json={"branch_id": A["branch_id"], "method": "COD"})
    client.script["place my order"] = {"review_ready": True, "answer": "Here's your order ... reply yes"}
    return say(client, "place my order").json()


def test_yes_places_the_order_and_keeps_the_id_hidden(client, monkeypatch):
    sent = backend(monkeypatch, httpx.Response(201, json={"orderId": "665f1c2e9b1e8a0012345678",
                                                          "orderNo": "CHZ-77"}))
    ready(client)
    r = say(client, "haan ji").json()
    assert "order is placed" in r["answer"].lower() or "place ho gaya" in r["answer"].lower()
    assert "665f1c2e" not in str(r) and "CHZ-77" not in str(r)  # the customer never sees the ids
    assert r["cart"] is None and r["order"]["type"] == "DELIVERY"
    assert sent[0]["order_type"] == "DELIVERY" and sent[0]["delivery_location"] == SPOT
    placed = api.cart_store().placed_orders(OWNER)[0]
    assert placed["order_id"] == "665f1c2e9b1e8a0012345678" and placed["order_type"] == "DELIVERY"
    say(client, "mera order kahan hai")
    assert client.calls[-1]["tracked"][0]["order_id"] == "665f1c2e9b1e8a0012345678"  # the bot tracks by it
    assert len(sent) == 1 and say(client, "yes").json()["answer"] == "ok"  # a second yes is just a message


def test_no_or_another_message_does_not_place(client, monkeypatch):
    sent = backend(monkeypatch, httpx.Response(201, json={"orderId": "x"}))
    ready(client)
    assert "haven't placed" in say(client, "no").json()["answer"]
    ready(client)
    assert say(client, "add a drink too").json()["answer"] == "ok"  # moved on: the summary is dropped
    assert say(client, "yes").json()["answer"] == "ok" and sent == []


def test_yes_in_another_session_or_after_a_change_does_not_place(client, monkeypatch):
    sent = backend(monkeypatch, httpx.Response(201, json={"orderId": "x"}))
    ready(client)
    assert say(client, "yes", session="6" * 32).json()["answer"] == "ok" and sent == []
    ready(client)
    add(client)  # one more burger after seeing the summary (3 in all: ready() ran twice)
    r = say(client, "yes").json()
    assert r["answer"].startswith("Your order changed") and "3 × Bazinga Burger" in r["answer"] and sent == []
    say(client, "yes")  # the new summary is what a yes now places
    assert sent[0]["items"][0]["qty"] == 3


def test_backend_down_keeps_the_yes_open(client, monkeypatch):
    def down(url, json, headers, timeout):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(config, "ORDER_BACKEND_URL", "https://orders.example/x")
    monkeypatch.setattr(checkout.httpx, "post", down)
    ready(client)
    assert "couldn't reach" in say(client, "yes").json()["answer"]
    sent = backend(monkeypatch, httpx.Response(201, json={"orderId": "x"}))
    say(client, "yes")
    assert len(sent) == 1


@pytest.mark.parametrize("text,answer", [("yes", "yes"), ("Haan kar do", "yes"), ("ok please", "yes"), ("ہاں", "yes"),
                                         ("no", "no"), ("nahi abhi", "no"),
                                         ("yes but add a drink", None), ("is it spicy?", None)])
def test_reading_the_answer(text, answer):
    assert order_flow.answer_to_confirm(text) == answer


def test_summary_is_code_written():
    review = checkout.review(
        user_id=USER, branch_status={"branch": "Gulberg", "open_now": True, "pickup": True},
        cart={"lines": [{"name": "Bazinga Burger", "size": None, "qty": 2, "line_total": 1080, "available": True}],
              "subtotal": 1080},
        payment_config=CONFIG, payment_code="COP", order={"type": "PICKUP"})
    text = order_flow.summary_text(review, "latin")
    assert "2 × Bazinga Burger – Rs 1,080" in text and "Pickup from Gulberg" in text and "Cash on pickup" in text
    assert text.endswith('Tap Confirm order (or reply "yes") to place it, or tell me what to change.')


def test_review_checks_the_order_type():
    base = dict(user_id=USER, branch_status={"open_now": True, "pickup": False},
                cart={"lines": [{"name": "x", "available": True}], "subtotal": 900}, payment_config=CONFIG)
    codes = lambda **kw: [p["code"] for p in checkout.review(**base, **kw)["problems"]]
    assert codes(payment_code="COD", order=None) == ["no_order_type"]
    assert codes(payment_code="COP", order={"type": "PICKUP"}) == ["pickup_not_offered"]
    assert codes(payment_code="COD", order={"type": "DELIVERY"}) == ["no_delivery_location"]
    assert codes(payment_code="COP", order={"type": "DELIVERY", **SPOT}) == ["payment_method_wrong_type"]


# ---------- tracking ----------

def test_tracking_uses_the_hidden_id(monkeypatch):
    seen = []
    monkeypatch.setattr(tools.repo, "get_order_by_id",
                        lambda user, oid, no: seen.append((user, oid)) or {"order_no": "CHZ-77", "status": "PREPARING"})
    ctx = tools.RequestContext(USER, "x", tracked=({"order_id": "abc", "order_no": None},))
    r = tools.get_order_status(ctx, tools.OrderStatusArgs())
    assert r["order"]["status"] == "PREPARING" and seen == [(USER, "abc")]
    monkeypatch.setattr(tools.repo, "get_order_by_id", lambda user, oid, no: None)
    r = tools.get_order_status(ctx, tools.OrderStatusArgs())
    assert r["order"]["status"] == "SUBMITTED" and "abc" not in str(r)  # not accepted yet; id never shown


# ---------- fixed replies for order actions (the model invented totals and branches here) ----------

def test_payment_choice_reply_uses_the_tool_numbers():
    text = order_flow.action_text({"payment_method_selected": "COD", "name": "Cash on delivery", "pay_now": False,
                                   "cart_subtotal": 1120, "tax_rate": 16, "tax": 154, "total": 1120}, "latin")
    assert text.startswith("Done: you'll pay by Cash on delivery. Estimated total: Rs 1,120 (incl. 16% tax).")


def test_switch_replies():
    t = lambda **r: order_flow.action_text({"order_type_selected": "PICKUP", **r}, "latin")
    assert t() == "Done, your order is now for pickup."
    assert "pick a branch for pickup" in t(needs="pickup_branch")
    assert "Cash on delivery doesn't work for pickup" in t(payment_cleared="Cash on delivery")
    assert "allow your location" in order_flow.action_text({"order_type_selected": "DELIVERY", "needs": "location"},
                                                           "latin")
    wrong = order_flow.action_text({"error": "not_for_this_order_type", "requested": "Cash on delivery",
                                    "order_type": "PICKUP", "available": [{"name": "Cash on pickup"}]}, "latin")
    assert wrong.startswith("Cash on delivery isn't available for pickup.") and "1. Cash on pickup" in wrong
    assert order_flow.action_text({"error": "ambiguous_method"}, "latin") is None  # the model asks which one


# ---------- not ready, payment by number, Confirm / Cancel buttons ----------

def test_not_ready_is_code_written_and_offers_payment():
    review = {"ready": False, "problems": [{"code": "no_payment_method"}],
              "payment_options": [{"name": "Cash on delivery", "total": 1050, "tax_rate": 16, "pay_now": False},
                                  {"name": "Debit / credit card (online)", "total": 1050, "tax_rate": 5,
                                   "pay_now": True}]}
    text = order_flow.not_ready_text(review, "roman_urdu")
    assert text.startswith("Aap kaise pay karna chahenge?")  # nothing invented: just the choice that's missing
    assert "1. Cash on delivery – Rs 1,050 (16% tax samait)" in text and "branch" not in text.lower()
    both = order_flow.not_ready_text({**review, "problems": [{"code": "empty_cart"}, {"code": "no_payment_method"}]},
                                     "latin")
    assert both.startswith("Almost there! Before I can place your order:\n- your cart is empty")


def test_review_offers_the_payment_choices(monkeypatch):
    monkeypatch.setattr(tools.repo, "get_branch_status", lambda b: {"branch": "J3", "open_now": True})
    monkeypatch.setattr(tools.repo, "get_payment_config", lambda b: CONFIG)
    monkeypatch.setattr(tools.cart, "price_cart", lambda lines, b: {"lines": [{"name": "x", "available": True,
                                                                               "qty": 1, "line_total": 1050}],
                                                                    "subtotal": 1050})
    ctx = tools.RequestContext(USER, "x", order={"type": "DELIVERY", **SPOT})
    r, _ = tools.review_order(ctx, tools.NoArgs(), [{"item_id": "p"}])
    assert [p["code"] for p in r["problems"]] == ["no_payment_method"]
    assert {o["code"] for o in r["payment_options"]} == {"XPAY_CARD", "CARD_MACHINE", "COD"}  # delivery ones only
    out, _ = tools.set_payment_method(ctx, tools.PaymentArgs(method="cash on delivery"), [{"item_id": "p"}])
    assert out["review"]["ready"]  # choosing the method completes the order: the summary follows
    assert order_flow.action_text(out, "latin").startswith("Done: you'll pay by Cash on delivery.\n\nHere's your order:")


def test_number_picks_from_the_payment_list():
    from app.agent.graph import _listed_payments, _picked_payment
    listed = _listed_payments([{"ready": False, "problems": [], "payment_options": [{"name": "A"}, {"name": "B"}]}])
    assert listed == ["A", "B"] and _picked_payment("2", listed) == "B" and _picked_payment("pehla wala", listed) == "A"
    assert _picked_payment("3", listed) is None and _picked_payment("2", []) is None


def test_confirm_button(client, monkeypatch):
    sent = backend(monkeypatch, httpx.Response(201, json={"orderId": "665f1c2e9b1e8a0012345678"}))
    r = ready(client)
    assert r["confirm"] is True  # the widget shows Confirm / Cancel under this answer
    press = lambda answer, session=SESSION: client.post(
        "/api/chat/confirm", json={"branch_id": A["branch_id"], "session_id": session, "answer": answer})
    assert press("yes", session="6" * 32).status_code == 409  # another session's summary
    r = press("yes").json()
    assert "placed" in r["answer"] and r["confirm"] is False and len(sent) == 1 and "665f1c2e" not in str(r)
    assert press("yes").json()["detail"] == "nothing_to_confirm" and len(sent) == 1  # a double tap is one order


def test_cancel_button_and_moving_on(client, monkeypatch):
    sent = backend(monkeypatch, httpx.Response(201, json={"orderId": "x"}))
    ready(client)
    r = client.post("/api/chat/confirm", json={"branch_id": A["branch_id"], "session_id": SESSION, "answer": "no"})
    assert "haven't placed" in r.json()["answer"]
    ready(client)
    say(client, "add a drink too")  # moved on: the buttons on the old summary no longer place anything
    r = client.post("/api/chat/confirm", json={"branch_id": A["branch_id"], "session_id": SESSION, "answer": "yes"})
    assert r.status_code == 409 and sent == []


# ---------- tracking without the model ----------

@pytest.mark.parametrize("message,args", [("where is my order?", {}), ("mera order kahan hai", {}),
                                          ("order status 170225-13665", {"order_no": "170225-13665"})])
def test_tracking_is_a_code_lookup(message, args):
    from app.agent.intents import prefetch_tools
    assert prefetch_tools(message) == [("get_order_status", args)]


def test_status_is_code_written_without_ids():
    text = order_flow.action_text({"placed_in_this_chat": True, "order": {
        "status": "SUBMITTED", "order_type": "PICKUP", "branch": "Jallo Lahore"}}, "latin")
    assert text == ("Your pickup order from Jallo Lahore has reached our ordering system and is waiting for the "
                    "branch to accept it.")
    assert "CHZ" not in order_flow.action_text({"order": {"status": "PICKEDUP", "order_no": "CHZ-1"}}, "latin")
    assert order_flow.action_text({"order": {"status": "SOMETHING_NEW"}}, "latin") is None  # the model explains
    assert order_flow.action_text({"error": "not_logged_in"}, "latin") == "Please log in to see your orders."


def test_a_bare_number_keeps_the_conversations_language():
    from langchain_core.messages import HumanMessage
    from app.agent.graph import pre_checks
    out = pre_checks({"messages": [HumanMessage("1", id="m1")], "language": "roman_urdu"})
    assert out["language"] == "roman_urdu"


def test_payment_pick_skips_the_planner(monkeypatch):
    """A bare "1" must reach the payment pick even when the planner would call it off-topic."""
    from langchain_core.messages import HumanMessage
    from app.agent import graph as g
    monkeypatch.setattr(g, "_plan_message", lambda prompt: pytest.fail("no planner call for a list pick"))
    out = g.understand_node({"messages": [HumanMessage("1", id="m")], "turn_id": "m", "steps": [],
                             "language": "latin", "pay_offered": ["Cash on delivery", "Card"]})
    assert out.get("answer") is None and out["plan"] is None  # on to prefetch, which sets Cash on delivery


def test_size_question_is_code_written_when_nothing_was_added():
    from app.agent.graph import _fixed_answer
    asked = {"added": [], "cart": {"lines": []}, "problems": [
        {"error": "size_required", "item": "Chicken Tikka",
         "valid_sizes": [{"size": "Small", "price": 690}, {"size": "Large", "price": 1850}]}]}
    assert _fixed_answer([asked], "latin") == "Chicken Tikka comes in Small Rs 690, Large Rs 1,850. Which size would you like?"
    partly = {**asked, "added": [{"name": "Bazinga Burger", "qty": 1}],
              "cart": {"lines": [{"line_no": 1, "name": "Bazinga Burger", "qty": 1, "line_total": 560}], "subtotal": 560}}
    text = _fixed_answer([partly], "latin")  # what was added, the cart, then the question: all code's words
    assert text.startswith("Added 1 × Bazinga Burger to your cart.") and text.endswith("Which size would you like?")


def test_a_word_only_one_items_name_has_adds_it(monkeypatch):
    """ "ek bazinga bhi daal do": deals that mention Bazinga inside aren't a reason to ask "which one?"."""
    from types import SimpleNamespace
    import app.cart as cart
    burger = {"item_id": "b1", "name": "Bazinga Burger", "type": "burger", "available": True, "price": 560}
    deals = [{"item_id": f"d{n}", "name": name, "type": "deal", "available": True, "price": 2090}
             for n, name in enumerate(["Zalmi Meal", "Zalmi Meal", "Somewhat Amazing 1"])]
    monkeypatch.setattr(cart, "index", lambda: SimpleNamespace(corrections=lambda text: {}))
    monkeypatch.setattr(cart, "_resolve", lambda arg, b: (None, {"error": "no_exact_match"}))
    monkeypatch.setattr(cart, "_options", lambda said, item, b, offered: cart._as_options(deals + [burger]))
    monkeypatch.setattr(cart.repo, "get_items", lambda ids, b, detailed=False: [burger] if ids == ["b1"] else [])
    monkeypatch.setattr(cart.repo, "merge_duplicates", lambda items: items)
    ctx = SimpleNamespace(user_message="ek bazinga bhi daal do", offered=(), branch_id="x")
    assert cart.resolve_for_chat(ctx, cart.CartItemArg(name="bazinga", qty=1)) == (burger, None)
    assert [o["name"] for o in cart._as_options(deals)] == ["Zalmi Meal", "Somewhat Amazing 1"]  # one card per name


def test_an_item_we_dont_have_gets_items_not_deals(monkeypatch):
    """ "ek pepsi": Soft Drink, not every deal that comes with a drink; one option -> "did you mean this one?"."""
    from types import SimpleNamespace
    import app.cart as cart
    from app.agent.graph import _cart_question
    soft = {"item_id": "s", "name": "Soft Drink", "type": "drink", "available": True, "price": 100}
    deal = {"item_id": "d", "name": "Zalmi Meal", "type": "deal", "available": True, "price": 2090}
    monkeypatch.setattr(cart, "index", lambda: SimpleNamespace(corrections=lambda text: {}))
    monkeypatch.setattr(cart, "_resolve", lambda arg, b: (None, {"error": "no_exact_match"}))
    monkeypatch.setattr(cart, "_options", lambda said, item, b, offered: cart._as_options([soft, deal]))
    monkeypatch.setattr(cart.repo, "merge_duplicates", lambda items: items)
    ctx = SimpleNamespace(user_message="ek pepsi bhi add kar do", offered=(), branch_id="x")
    _, problem = cart.resolve_for_chat(ctx, cart.CartItemArg(name="pepsi", qty=1))
    assert [o["name"] for o in problem["options"]] == ["Soft Drink"]
    asked = _cart_question({"added": [], "problems": [problem]}, "latin")
    assert asked.startswith("We don't have exactly that. Did you mean this one?")
