"""A Mongo failure anywhere in a turn must give one clear error: no partial answer, no memory, cart undone."""
import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from pymongo.errors import AutoReconnect

import app.agent.graph as g
import app.data.db as db
import app.tools as tools

CART = [{"item_id": "i1", "name": "Chicken Tikka", "size": "Regular", "qty": 1}]


def down(*a, **k):
    raise AutoReconnect("SSL handshake failed")


class FakeIndex:
    def search(self, *a, **k):
        return [{"item_id": "i1"}]

    def keyword_matches(self, *a, **k):
        return []


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    monkeypatch.setattr(g.repo, "get_branch_status", lambda b: {"branch": "Gulberg", "city": "Lahore"})
    monkeypatch.setattr(g, "price_cart", lambda cart, b: {"lines": [], "item_count": 0, "subtotal": 0})
    monkeypatch.setattr(tools, "index", lambda: FakeIndex())
    monkeypatch.setattr(g, "_trace", lambda r: None)
    calls = iter([AIMessage(content="", tool_calls=[{"name": "search_menu", "args": {"query": "tikka"}, "id": "c1"}]),
                  AIMessage(content="Chicken Tikka is great.")])
    monkeypatch.setattr(g, "_invoke_llm", lambda msgs, allow_tools: next(calls))

    graph = g.build_graph(InMemorySaver())
    cfg = {"configurable": {"thread_id": "t1", "user_id": None, "branch_id": "b1"}}
    graph.update_state(cfg, {"cart": CART})

    def turn(msg="how much is chicken tikka"):
        out = graph.invoke({"messages": [g.HumanMessage(content=msg)]}, config=cfg)
        return out["response"], graph.get_state(cfg).values

    return turn


def assert_clean_error(response, state):
    assert response["error"] == "database_unavailable"
    assert response["answer"] == g.DB_DOWN_ANSWER and response["items"] == [] and response["cart"] is None
    assert state.get("messages", []) == []  # turn forgotten: "Try again" starts clean
    assert state["cart"] == CART  # cart as before the turn


def test_tool_db_failure_ends_turn(run, monkeypatch):
    monkeypatch.setattr(tools.repo, "get_items", down)
    assert_clean_error(*run())


def test_system_prompt_db_failure_ends_turn(run, monkeypatch):
    monkeypatch.setattr(g.repo, "get_branch_status", down)
    assert_clean_error(*run())


def test_cart_pricing_db_failure_ends_turn(run, monkeypatch):
    monkeypatch.setattr(tools.repo, "get_items", lambda ids, b, detailed=False: [])
    monkeypatch.setattr(g, "price_cart", down)
    assert_clean_error(*run())


def test_one_blip_is_retried_and_turn_succeeds(run, monkeypatch):
    flaky = iter([AutoReconnect("blip")])

    def get_items(ids, b, detailed=False):
        e = next(flaky, None)
        if e:
            raise e
        return [{"item_id": "i1", "name": "Chicken Tikka", "price": 1290, "available": True}]

    monkeypatch.setattr(tools.repo, "get_items", get_items)
    response, state = run()
    assert "error" not in response and response["answer"] == "Chicken Tikka is great."
    assert len(state["messages"]) == 2  # question + answer kept


def test_chat_stream_reports_progress_then_response(monkeypatch):
    monkeypatch.setattr(g.repo, "get_branch_status", lambda b: {"branch": "Gulberg", "city": "Lahore"})
    monkeypatch.setattr(g, "price_cart", lambda cart, b: {"lines": [], "item_count": 0, "subtotal": 0})
    monkeypatch.setattr(tools, "index", lambda: FakeIndex())
    monkeypatch.setattr(tools.repo, "get_items",
                        lambda ids, b, detailed=False: [{"item_id": "i1", "name": "Chicken Tikka", "price": 1290,
                                                         "available": True}])
    monkeypatch.setattr(g, "_trace", lambda r: None)
    calls = iter([AIMessage(content="", tool_calls=[{"name": "search_menu", "args": {"query": "tikka"}, "id": "c1"}]),
                  AIMessage(content="Chicken Tikka is Rs 1290.")])
    monkeypatch.setattr(g, "_invoke_llm", lambda msgs, allow_tools: next(calls))
    monkeypatch.setattr(g, "graph", lambda: g.build_graph(InMemorySaver()))

    events = list(g.chat_stream("tell me about chicken tikka", thread_id="s1", branch_id="b1"))  # no prefetch: model searches
    assert [e["status"] for e in events[:-1]] == ["Thinking…", "Searching the menu…"]
    assert events[-1]["response"]["answer"] == "Chicken Tikka is Rs 1290."


def test_time_budget_with_pending_tool_call_ends_safely(monkeypatch):
    """Regression: a turn over the time budget whose model still wanted a tool looped to the recursion limit."""
    monkeypatch.setattr(g.repo, "get_branch_status", lambda b: {"branch": "Gulberg", "city": "Lahore"})
    monkeypatch.setattr(g, "price_cart", lambda cart, b: {"lines": [], "item_count": 0, "subtotal": 0})
    monkeypatch.setattr(g, "_trace", lambda r: None)
    monkeypatch.setattr(g, "TIME_BUDGET_S", -1)  # already over budget
    monkeypatch.setattr(g, "_invoke_llm", lambda msgs, allow_tools: AIMessage(
        content="", tool_calls=[{"name": "search_menu", "args": {"query": "tikka"}, "id": "c1"}]))
    graph = g.build_graph(InMemorySaver())
    out = graph.invoke({"messages": [g.HumanMessage(content="chicken tikka")]},
                       config={"configurable": {"thread_id": "slow", "user_id": None, "branch_id": "b1"},
                               "recursion_limit": 25})
    assert out["response"]["answer"] == g.SAFE_FALLBACK


def test_offline_cart_reply_is_honest(monkeypatch):
    """Regression: when the LLM failed after add_to_cart added NOTHING, the bot said 'Done, your cart is updated'."""
    ctx = tools.RequestContext(None, "b1")
    monkeypatch.setattr(g, "price_cart", lambda cart, b: {"lines": [], "item_count": 0, "subtotal": 0})
    state = {"tools_called": ["add_to_cart"], "cart": [], "cart_before": [],
             "tool_results": [{"added": [], "problems": [{"error": "size_required", "item": "Black Pepper Tikka",
                                                          "valid_sizes": [{"size": "Small", "price": 690}]}]}]}
    r = g._offline_response(ctx, "add black pepper tikka", state)
    assert "updated" not in r["answer"] and "Small Rs 690" in r["answer"]
    state["tool_results"] = [{"added": [], "problems": [{"error": "not_found"}]}]
    assert "couldn't update" in g._offline_response(ctx, "add x", state)["answer"]
    state.update(cart=[{"item_id": "i1", "name": "X", "size": None, "qty": 1}])
    assert "Done, your cart is updated" in g._offline_response(ctx, "add x", state)["answer"]
