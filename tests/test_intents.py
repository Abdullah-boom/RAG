"""Obvious questions get their tool run before the model writes (small models skip it otherwise)."""
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

import app.agent.graph as g
from app.agent.intents import prefetch_tools


@pytest.mark.parametrize("message,expected", [
    ("What's in my cart?", ["view_cart"]),
    ("cart dikhao", ["view_cart"]),
    ("mera total kitna hai", ["view_cart"]),
    ("Show my cart", ["view_cart"]),
    ("Do you deliver and what are your timings?", ["check_branch_status"]),
    ("branch kab tak khula hai", ["check_branch_status"]),
    ("Any deals for 2 people under 2500?", ["search_menu"]),  # deals only, capped at the budget
    ("koi offer hai?", ["get_deals"]),
    ("ma apni girlfriend to kiya khilaon", ["get_best_sellers"]),
    ("What's popular here?", ["get_best_sellers"]),
    ("any deals and are you open?", ["get_deals", "check_branch_status"]),
])
def test_prefetch_detects_obvious_tools(message, expected):
    assert [name for name, _ in prefetch_tools(message)] == expected


@pytest.mark.parametrize("message,query", [
    ("pizza kitni ki hai?", "pizza"),
    ("how much is chicken tikka", "chicken tikka"),
    ("what is the price of bazinga burger", "bazinga burger"),
    ("Large Crown Crust kitne ki hai bhai", "Large Crown Crust"),
])
def test_plain_price_question_searches_the_item(message, query):
    assert prefetch_tools(message)[0] == ("search_menu", {"query": query})


@pytest.mark.parametrize("message", ["kitne ka hai", "add 2 pizza kitne ka hoga", "mera total kitna hai",
                                     "how much would 2 large pizzas and 3 burgers with drinks cost"])
def test_no_price_search_when_item_unclear_or_cart(message):
    assert "search_menu" not in [name for name, _ in prefetch_tools(message)]


@pytest.mark.parametrize("message", [
    "Add one Reggy Burger to my cart",      # changing the cart needs arguments: the model's job
    "remove the pizza from my cart",
    "cart khali kar do",
    "tell me about the bazinga burger",
    "hello",
])
def test_prefetch_leaves_the_rest_to_the_model(message):
    assert "view_cart" not in [name for name, _ in prefetch_tools(message)]
    if message in ("tell me about the bazinga burger", "hello"):
        assert prefetch_tools(message) == []


def test_prefetched_cart_reaches_the_model_and_guard(monkeypatch):
    cart = [{"item_id": "p1", "name": "Reggy Burger", "size": None, "qty": 3}]
    priced = {"lines": [{"line_no": 1, "name": "Reggy Burger", "size": None, "qty": 3, "unit_price": 380,
                         "line_total": 1140, "available": True}], "item_count": 3, "subtotal": 1140}
    import app.cart as cart_mod
    monkeypatch.setattr(cart_mod, "price_cart", lambda lines, b: priced)
    monkeypatch.setattr(g, "price_cart", lambda lines, b: priced)
    monkeypatch.setattr(g.repo, "get_branch_status", lambda b: {"branch": "I-8", "city": "Islamabad"})
    monkeypatch.setattr(g, "_trace", lambda r: None)
    seen = []

    def fake_llm(msgs, allow_tools):
        seen.append(msgs)
        return AIMessage(content="Your cart has 3 Reggy Burger, subtotal Rs 1140.")

    monkeypatch.setattr(g, "_invoke_llm", fake_llm)
    graph = g.build_graph(InMemorySaver())
    cfg = {"configurable": {"thread_id": "t", "user_id": None, "branch_id": "b1"}}
    out = graph.invoke({"messages": [g.HumanMessage(content="What's in my cart?")], "cart": cart}, config=cfg)
    tool_msgs = [m for m in seen[0] if isinstance(m, ToolMessage)]
    assert [m.name for m in tool_msgs] == ["view_cart"] and "1140" in tool_msgs[0].content
    assert len(seen) == 1  # one model call: no extra round trip, and the guard accepted the answer
    assert out["response"]["answer"].startswith("Your cart has 3 Reggy Burger")
    assert out["response"]["cart_lines"] == cart


def _run_turn(monkeypatch, message, drafts):
    monkeypatch.setattr(g.repo, "get_branch_status", lambda b: {"branch": "I-8", "city": "Islamabad"})
    monkeypatch.setattr(g, "price_cart", lambda lines, b: {"lines": [], "item_count": 0, "subtotal": 0})
    monkeypatch.setattr(g, "_trace", lambda r: None)
    seen, replies = [], iter(drafts)

    def fake_llm(msgs, allow_tools):
        seen.append(msgs)
        return AIMessage(content=next(replies))

    monkeypatch.setattr(g, "_invoke_llm", fake_llm)
    graph = g.build_graph(InMemorySaver())
    cfg = {"configurable": {"thread_id": "t", "user_id": None, "branch_id": "b1"}}
    out = graph.invoke({"messages": [g.HumanMessage(content=message)], "cart": []}, config=cfg)
    return out["response"], seen, graph.get_state(cfg).values


def test_roman_urdu_note_reaches_model_but_not_memory(monkeypatch):
    english = "Your cart is empty right now, you can add items by telling me what you would like to order."
    response, seen, state = _run_turn(monkeypatch, "cart dikhao", [english, english])
    human = [m for m in seen[0] if isinstance(m, g.HumanMessage)][-1]
    assert human.content == f"cart dikhao\n\n{g.REPLY_NOTES['roman_urdu']}"
    assert [m.content for m in state["messages"] if isinstance(m, g.HumanMessage)] == ["cart dikhao"]
    # facts were verified and only the language stayed wrong after the retry: answer, don't fall back
    assert response["answer"] == english


def test_wrong_facts_still_fall_back(monkeypatch):
    invented = "Chicken Tikka pizza is Rs 999 and Behari Kebab pizza is Rs 888, which one would you like?"
    response, _, _ = _run_turn(monkeypatch, "hello there friend", [invented, invented])
    assert response["answer"] == g.SAFE_FALLBACK


def test_garbled_urdu_retry_falls_back_to_the_readable_draft(monkeypatch):
    english = "Your cart is empty right now, you can add items by telling me what you would like to order."
    garbled = "Ji, yہ رہی آپکی کارت۔ حالیے میں اس میں کچھ نہیں ہے۔ آپ کو اہم طور پر مہم کرتے ہے"
    response, _, _ = _run_turn(monkeypatch, "cart dikhao", [english, garbled])
    assert response["answer"] == english


@pytest.mark.parametrize("message", ["koi achi cheez batao", "kuch mazedar khilao", "what is good here",
                                     "anything tasty?", "kya acha hai yahan", "mujhe kuch acha chahiye"])
def test_loose_recommendation_requests(message):
    assert prefetch_tools(message) == [("get_best_sellers", {})]


@pytest.mark.parametrize("message,expected", [
    ("Good morning, are you open?", ["check_branch_status"]),  # a greeting is not "something good"
    ("good evening", []),
    ("chicken tikka acha hai?", []),                            # about one item: the model searches it
    ("koi achi deal batao", ["get_deals"]),
])
def test_quality_words_that_are_not_recommendations(message, expected):
    assert [name for name, _ in prefetch_tools(message)] == expected


def test_failed_turn_answers_in_customer_language_with_found_cards(monkeypatch):
    import app.tools as tools_mod
    best = {"item_id": "p9", "name": "Stuff Crust Pizza", "type": "pizza", "available": True, "price": None,
            "price_from": 1530, "sizes": [{"size": "Regular", "price": 1530}]}
    monkeypatch.setattr(tools_mod.repo, "get_best_seller_ids", lambda b: ["p9"])
    monkeypatch.setattr(tools_mod.repo, "get_items", lambda ids, b, detailed=False: [dict(best)])
    invented = "Aap ke liye Chicken Tikka pizza Rs 999 aur Behari Kebab pizza Rs 888 hai, kaunsa add karun?"
    response, _, _ = _run_turn(monkeypatch, "koi achi cheez batao", [invented, invented])
    assert response["answer"] == g.FOUND_FALLBACKS["roman_urdu"]
    assert [c["name"] for c in response["items"]] == ["Stuff Crust Pizza"]


def test_failed_turn_without_findings_is_localized(monkeypatch):
    invented = "Chicken Tikka pizza Rs 999 hai aur Behari Kebab pizza Rs 888 hai, aap kaunsa lena chahenge bhai?"
    response, _, _ = _run_turn(monkeypatch, "bhai kuch batao na yaar", [invented, invented])
    assert response["answer"] == g.FALLBACKS["roman_urdu"] and response["items"] == []


@pytest.mark.parametrize("message,expected", [
    ("how can I pay?", [("get_payment_methods", {})]),
    ("card se pay ho sakta hai?", [("get_payment_methods", {})]),
    ("is cash on delivery available?", [("get_payment_methods", {})]),
    ("cash on delivery karunga", [("set_payment_method", {"method": "cash on delivery karunga"})]),
    ("I'll pay by online card", [("set_payment_method", {"method": "I'll pay by online card"})]),
])
def test_payment_question_vs_choice(message, expected):
    assert prefetch_tools(message) == expected


@pytest.mark.parametrize("message,query", [
    ("burger kon kon sa hain", "burger"),
    ("kon kon se pizza hain", "pizza"),
    ("What burgers do you have?", "burgers"),
    ("burgers dikhao", "burgers"),
    ("do you have pasta?", "pasta"),
    ("which wings are available", "wings"),
])
def test_which_items_questions_search_that_kind(message, query):
    assert prefetch_tools(message) == [("search_menu", {"query": query})]


@pytest.mark.parametrize("message,expected", [("koi deal dikhao", ["get_deals"]), ("cart dikhao", ["view_cart"]),
                                              ("show me the menu", [])])
def test_listing_words_that_mean_something_else(message, expected):
    assert [name for name, _ in prefetch_tools(message)] == expected


@pytest.mark.parametrize("message,item", [
    ("add pasta to cart", {"name": "pasta", "qty": 1}),
    ("add 2 bazinga burger", {"name": "bazinga burger", "qty": 2}),
    ("pehli wali", {"name": "pehli wali", "qty": 1}),
    ("large", {"name": "large", "qty": 1, "size": "Large"}),
])
def test_adding_runs_add_to_cart_with_customer_words(message, item):
    assert prefetch_tools(message) == [("add_to_cart", {"items": [item]})]


@pytest.mark.parametrize("message", ["2 bazinga aur 1 pasta add karo", "remove pasta", "add card payment"])
def test_adding_left_to_model(message):
    assert "add_to_cart" not in [name for name, _ in prefetch_tools(message)]


@pytest.mark.parametrize("message", ["which burger did you mention first?", "what was the second one?",
                                     "pehli wali kitne ki hai?"])
def test_questions_about_shown_items_never_add(message):
    assert "add_to_cart" not in [name for name, _ in prefetch_tools(message)]


@pytest.mark.parametrize("message", ["pehli wali", "the second one please", "pehli wali add karo"])
def test_choosing_a_shown_item_adds(message):
    assert [name for name, _ in prefetch_tools(message)] == ["add_to_cart"]


@pytest.mark.parametrize("message,args", [
    ("Kuch teekha 800 se kam", {"query": "teekha", "max_price": 800}),
    ("spicy pizza under 1500", {"query": "spicy pizza", "max_price": 1500}),
    ("koi meetha 500 tak", {"query": "meetha", "max_price": 500}),
    ("1000 tak kya milega", {"query": "popular", "max_price": 1000}),
    ("deal under 2000", {"query": "deal", "max_price": 2000, "deals_only": True}),
])
def test_budget_questions_search_with_a_price_limit(message, args):
    assert prefetch_tools(message) == [("search_menu", args)]
