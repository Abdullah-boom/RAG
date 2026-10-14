"""The chat adds an item only when the CUSTOMER chose it: their own words, or pointing at what was just shown."""
import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

import app.agent.graph as g
import app.cart as cart
from app.tools import RequestContext

PASTAS = {"f1": {"item_id": "f1", "name": "Fettuccine Alfredo Pasta", "type": "pasta", "price": 1050, "available": True},
          "c1": {"item_id": "c1", "name": "Crunchy Chicken Pasta", "type": "pasta", "price": 950, "available": True},
          "b1": {"item_id": "b1", "name": "Bazinga Burger", "type": "burger", "price": 560, "available": True}}


class Idx:
    def search(self, query, kind, k=8, branch_id=None, **kw):
        q = query.lower()
        return [{"item_id": i} for i, it in PASTAS.items()
                if any(w in it["name"].lower() or w == it["type"] for w in q.split())]

    def corrections(self, text):
        return {"fetuccine": "fettuccine"} if "fetuccine" in text else {}


@pytest.fixture(autouse=True)
def menu(monkeypatch):
    monkeypatch.setattr(cart, "index", lambda: Idx())
    monkeypatch.setattr(cart.repo, "get_items", lambda ids, b, detailed=False: [dict(PASTAS[i]) for i in ids if i in PASTAS])
    monkeypatch.setattr(cart.repo, "merge_duplicates", lambda items: list({i["item_id"]: i for i in items}.values()))
    monkeypatch.setattr(cart, "price_cart", lambda lines, b: {"lines": lines, "subtotal": 0})


def add(said, name="Fettuccine Alfredo Pasta", offered=()):
    ctx = RequestContext(None, "b", user_message=said, offered=tuple(offered))
    return cart.add_to_cart(ctx, cart.AddToCartArgs(items=[cart.CartItemArg(name=name, qty=1)]), [])


def test_kind_only_asks_which_one_even_if_model_picked():
    """The bug: "add pasta to cart" -> the model passed "Fettuccine Alfredo Pasta" and it went straight in."""
    result, lines = add("add pasta to cart")
    assert lines == [] and not result["added"]
    problem = result["problems"][0]
    assert problem["error"] == "which_one"
    assert {o["name"] for o in problem["options"]} == {"Fettuccine Alfredo Pasta", "Crunchy Chicken Pasta"}
    assert {i["name"] for i in result["items"]} == {"Fettuccine Alfredo Pasta", "Crunchy Chicken Pasta"}  # cards


@pytest.mark.parametrize("said", ["add fettuccine", "alfredo wala pasta daal do", "add fetuccine to cart"])
def test_customer_named_it(said):
    result, lines = add(said)
    assert [l["name"] for l in lines] == ["Fettuccine Alfredo Pasta"]


SHOWN = [{"item_id": "c1", "name": "Crunchy Chicken Pasta", "type": "pasta"},
         {"item_id": "f1", "name": "Fettuccine Alfredo Pasta", "type": "pasta"}]


@pytest.mark.parametrize("said,expected", [
    ("pehli wali add karo", "Crunchy Chicken Pasta"),
    ("the second one", "Fettuccine Alfredo Pasta"),
    ("option 2 please", "Fettuccine Alfredo Pasta"),
    ("last one", "Fettuccine Alfredo Pasta"),
])
def test_customer_pointed_at_a_shown_item(said, expected):
    # the model passes a wrong name: what the customer pointed at wins
    result, lines = add(said, name="Bazinga Burger", offered=SHOWN)
    assert [l["name"] for l in lines] == [expected]


def test_add_it_when_one_item_was_shown():
    _, lines = add("haan isko add kar do", name="Crunchy Chicken Pasta", offered=SHOWN[:1])
    assert [l["name"] for l in lines] == ["Crunchy Chicken Pasta"]
    result, lines = add("add it", offered=SHOWN)  # two shown: "it" is unclear
    assert lines == [] and result["problems"][0]["error"] == "which_one"


def test_only_one_of_that_kind_needs_no_question():
    result, lines = add("add burger", name="Bazinga Burger")
    assert [l["name"] for l in lines] == ["Bazinga Burger"]


def test_card_button_skips_the_check():
    ctx = RequestContext(None, "b")  # API button: no chat message
    _, lines = cart.add_to_cart(ctx, cart.AddToCartArgs(items=[cart.CartItemArg(item_id="f1", qty=1)]), [])
    assert [l["name"] for l in lines] == ["Fettuccine Alfredo Pasta"]


def test_graph_remembers_what_it_offered(monkeypatch):
    """Turn 1 asks which pasta (options become cards and `offered`); turn 2 "pehli wali" adds the first."""
    monkeypatch.setattr(g.repo, "get_branch_status", lambda b: {"branch": "Gulberg", "city": "Lahore"})
    monkeypatch.setattr(g, "price_cart", lambda lines, b: {"lines": [], "item_count": 0, "subtotal": 0})
    monkeypatch.setattr(g, "_trace", lambda r: None)
    monkeypatch.setattr(g.repo, "merge_duplicates", lambda items: list({i["item_id"]: i for i in items}.values()))
    call = lambda name: AIMessage(content="", tool_calls=[{"name": "add_to_cart", "id": "a",
                                                          "args": {"items": [{"name": name, "qty": 1}]}}])
    replies = iter([call("Fettuccine Alfredo Pasta"),
                    AIMessage(content="Which pasta would you like: Crunchy Chicken Pasta or Fettuccine Alfredo Pasta?"),
                    call("Fettuccine Alfredo Pasta"),  # the model guesses wrong again
                    AIMessage(content="Added Crunchy Chicken Pasta to your cart.")])
    monkeypatch.setattr(g, "_invoke_llm", lambda msgs, allow_tools: next(replies))
    graph = g.build_graph(InMemorySaver())
    cfg = {"configurable": {"thread_id": "t", "user_id": None, "branch_id": "b"}}
    out = graph.invoke({"messages": [g.HumanMessage(content="add pasta to cart")], "cart": []}, config=cfg)
    assert out["response"]["cart_lines"] == []
    first_card = out["response"]["items"][0]["name"]
    out = graph.invoke({"messages": [g.HumanMessage(content="pehli wali")]}, config=cfg)
    assert [l["name"] for l in out["response"]["cart_lines"]] == [first_card]


@pytest.mark.parametrize("said", ["pehli wali", "add it"])
def test_pointing_at_nothing_asks(said):
    result, lines = add(said)
    assert lines == [] and result["problems"][0]["error"] == "unclear_which_item"


def test_size_with_several_shown_asks_which_of_them():
    result, lines = add("large", offered=SHOWN)
    assert lines == [] and {o["name"] for o in result["problems"][0]["options"]} == {o["name"] for o in SHOWN}


def test_size_reply_after_size_question():
    sized = {"item_id": "z1", "name": "Crown Crust", "type": "pizza", "available": True, "price": None,
             "sizes": [{"size": "Regular", "price": 1550}, {"size": "Large", "price": 2050}]}
    PASTAS["z1"] = sized
    try:
        result, lines = add("add crown crust", name="Crown Crust")
        assert lines == [] and result["problems"][0]["error"] == "size_required"
        assert [i["name"] for i in result["items"]] == ["Crown Crust"]  # shown, so "large" means it next turn
        _, lines = add("large wala", name="Crown Crust", offered=[{"item_id": "z1", "name": "Crown Crust"}])
        assert lines == [{"item_id": "z1", "name": "Crown Crust", "size": "Large", "qty": 1}]
    finally:
        PASTAS.pop("z1")


def test_model_cannot_pick_a_size_for_the_customer():
    sized = {"item_id": "z1", "name": "Crown Crust", "type": "pizza", "available": True, "price": None,
             "sizes": [{"size": "Regular", "price": 1550}, {"size": "Large", "price": 2050}]}
    PASTAS["z1"] = sized
    try:
        ctx = RequestContext(None, "b", user_message="add crown crust")
        args = cart.AddToCartArgs(items=[cart.CartItemArg(name="Crown Crust", size="Regular", qty=1)])
        result, lines = cart.add_to_cart(ctx, args, [])
        assert lines == [] and result["problems"][0]["error"] == "size_required"
        _, lines = add("bara wala crown crust", name="Crown Crust")
        assert lines[0]["size"] == "Large"
    finally:
        PASTAS.pop("z1")


def test_naming_one_shown_item_is_choosing_it():
    assert cart.chosen_from_offered("fettuccine wala", tuple(SHOWN))["item_id"] == "f1"
    assert cart.chosen_from_offered("pasta", tuple(SHOWN)) is None  # both are pasta


def test_shown_list_survives_a_question_but_not_a_cart_change(monkeypatch):
    """Burgers shown -> "pehli wali kitne ki hai?" looks up the first (no add) -> "pehli wali add karo" adds it ->
    after the cart changed, "add it" has nothing to point at."""
    monkeypatch.setattr(g.repo, "get_branch_status", lambda b: {"branch": "Gulberg", "city": "Lahore"})
    monkeypatch.setattr(g, "price_cart", lambda lines, b: {"lines": [], "item_count": 0, "subtotal": 0})
    monkeypatch.setattr(g, "_trace", lambda r: None)
    monkeypatch.setattr(g.repo, "merge_duplicates", lambda items: list({i["item_id"]: i for i in items}.values()))
    monkeypatch.setattr(g, "_invoke_llm", lambda msgs, allow_tools: AIMessage(content="Okay."))
    graph = g.build_graph(InMemorySaver())
    cfg = {"configurable": {"thread_id": "t", "user_id": None, "branch_id": "b"}}
    graph.update_state(cfg, {"offered": [{"item_id": "c1", "name": "Crunchy Chicken Pasta", "type": "pasta"},
                                         {"item_id": "f1", "name": "Fettuccine Alfredo Pasta", "type": "pasta"}],
                             "cart": []})
    out = graph.invoke({"messages": [g.HumanMessage(content="pehli wali kitne ki hai?")]}, config=cfg)
    assert out["response"]["cart_lines"] == []
    out = graph.invoke({"messages": [g.HumanMessage(content="pehli wali add karo")]}, config=cfg)
    assert [l["name"] for l in out["response"]["cart_lines"]] == ["Crunchy Chicken Pasta"]
    out = graph.invoke({"messages": [g.HumanMessage(content="add it")]}, config=cfg)
    assert [l["name"] for l in out["response"]["cart_lines"]] == ["Crunchy Chicken Pasta"]  # nothing more added
