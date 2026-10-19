"""Understanding layer (L2) and workflow router (L3): plan parsing, routing rules, and the graph using them."""
import json

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agent import graph as g
from app.agent import understand as u
from app.agent.understand import Plan, PlanItem, parse, route

OFFERED = ({"item_id": "a", "name": "Crown Crust", "type": "pizza"}, {"item_id": "b", "name": "Bazinga", "type": "burger"})


# ---------- parse ----------

def test_parse_tolerates_fences_nulls_and_bare_items():
    plan = parse('```json\n{"intent":"add","items":["bazinga",{"name":"crown crust","qty":1,"size":null}],'
                 '"budget":"null","language":"roman_urdu"}\n```')
    assert plan.intent == "add" and plan.budget is None
    assert [i.name for i in plan.items] == ["bazinga", "crown crust"] and plan.items[1].size is None


def test_parse_rejects_garbage_and_unknown_intents():
    assert parse("I think the customer wants pizza") is None
    assert parse('{"intent":"dance"}') is None


def test_schema_has_no_titles_and_lists_every_intent():
    s = u.schema()
    assert "title" not in json.dumps(s)
    assert "off_topic" in json.dumps(s) and "item_info" in json.dumps(s)


def test_missing_item_only_when_no_result_shares_a_distinctive_word():
    burgers = [{"name": "Bazooka"}, {"name": "Bazinga Burger"}]
    assert u.missing_item("sushi", burgers, {})
    assert not u.missing_item("chicken tiqqa", [{"name": "Chicken Tikka"}], {"tiqqa": "tikka"})  # a typo, not missing
    assert not u.missing_item("bazinga", burgers, {})
    assert not u.missing_item("chicken burger", burgers, {})  # only generic words: nothing to be missing
    assert not u.missing_item("sushi", [], {})  # no results: the no_match path handles it


# ---------- route ----------

def test_clear_intents_run_their_tool():
    cases = {"view_cart": "view_cart", "branch": "check_branch_status", "checkout": "review_order",
             "payment_options": "get_payment_methods", "order_history": "get_order_history"}
    for intent, tool in cases.items():
        calls, topics = route(Plan(intent=intent), "x")
        assert [c[0] for c in calls] == [tool] and "menu" in topics


def test_multi_item_add_keeps_qty_and_typed_size():
    plan = Plan(intent="add", items=[PlanItem(name="bazinga", qty=2), PlanItem(name="crown crust", size="Large")])
    calls, topics = route(plan, "2 bazinga aur 1 large crown crust daal do")
    assert calls == [("add_to_cart", {"items": [{"name": "bazinga", "qty": 2},
                                                {"name": "crown crust", "qty": 1, "size": "Large"}]})]
    assert topics == ["menu", "cart"]


def test_add_drops_sizes_the_customer_never_typed():
    plan = Plan(intent="add", items=[PlanItem(name="crown crust", size="Pizza"), PlanItem(name="bazinga", size="Large")])
    calls, _ = route(plan, "crown crust aur bazinga daal do")
    assert calls[0][1]["items"] == [{"name": "crown crust", "qty": 1}, {"name": "bazinga", "qty": 1}]


def test_add_without_evidence_goes_to_the_agent():
    """"4 log hain kya order karein" read as add "order" x4: no add/want word, no item -> nothing is added."""
    calls, topics = route(Plan(intent="add", items=[PlanItem(name="order", qty=4)]), "4 log hain kya order karein")
    assert calls == [] and topics == ["menu", "cart"]


def test_pointing_is_resolved_by_cart_code_not_the_model():
    """The model listed both shown items for "dusri wali add kardo"; the cart code reads the pointer."""
    plan = Plan(intent="add", items=[PlanItem(name="crown crust"), PlanItem(name="bazinga")])
    calls, _ = route(plan, "dusri wali add kardo", OFFERED)
    assert calls == [("add_to_cart", {"items": [{"name": "dusri wali", "qty": 1}]})]


def test_price_question_about_a_shown_item_is_never_an_add():
    calls, topics = route(Plan(intent="add", items=[PlanItem(name="Crown Crust")]), "pehli wali kitne ki hai", OFFERED)
    assert calls == [("search_menu", {"query": "Crown Crust", "picked": True})] and topics == ["menu"]


def test_craving_searches_the_english_wish_with_budget_only_when_given():
    assert route(Plan(intent="recommend", wish="sour"), "kuch khatta ha?")[0] == [("search_menu", {"query": "sour"})]
    assert route(Plan(intent="recommend", wish="spicy", budget=1000), "1000 se kam mein kuch spicy")[0] == \
        [("search_menu", {"query": "spicy", "max_price": 1000})]
    assert route(Plan(intent="recommend"), "koi achi cheez")[0] == [("get_best_sellers", {})]
    assert route(Plan(intent="recommend", wish="deal"), "4 log hain")[0] == [("get_deals", {})]


def test_browse_with_nothing_named_shows_best_sellers():
    assert route(Plan(intent="browse"), "dikha")[0] == [("get_best_sellers", {})]


def test_open_ended_intents_leave_tools_to_the_agent():
    assert route(Plan(intent="remove", items=[PlanItem(name="bazinga")]), "bazinga hata do") == ([], ["menu", "cart"])
    calls, topics = route(Plan(intent="unclear"), "hmm")
    assert calls == [] and set(topics) == set(g.ALL_TOPICS)


# ---------- the graph with a plan ----------

def _run(monkeypatch, message, plan, drafts, results=None, offered=None, traces=None):
    monkeypatch.setattr(g.repo, "get_branch_status", lambda b: {"branch": "I-8", "city": "Islamabad"})
    monkeypatch.setattr(g, "price_cart", lambda lines, b: {"lines": [], "item_count": 0, "subtotal": 0})
    monkeypatch.setattr(g, "_trace", (traces if traces is not None else []).append)
    monkeypatch.setattr(g, "_plan_message", lambda prompt: plan)
    ran, seen, replies = [], [], iter(drafts)
    results = results or {}

    def fake_tool(ctx, name, raw_args, cart_lines):
        ran.append((name, json.loads(raw_args)))
        return results.get(name, {"items": []}), cart_lines

    def fake_llm(msgs, allow_tools):
        seen.append((msgs, allow_tools))
        return AIMessage(content=next(replies))

    monkeypatch.setattr(g, "run_tool_with_cart", fake_tool)
    monkeypatch.setattr(g, "_invoke_llm", fake_llm)
    graph = g.build_graph(InMemorySaver())
    cfg = {"configurable": {"thread_id": "t", "user_id": None, "branch_id": "b1"}}
    state = {"messages": [g.HumanMessage(content=message)], "cart": []}
    if offered:
        state["offered"] = list(offered)
    out = graph.invoke(state, config=cfg)
    return out["response"], ran, seen


def test_off_topic_plan_gets_the_fixed_reply_without_tools(monkeypatch):
    response, ran, seen = _run(monkeypatch, "2+2 kitne hote hain", Plan(intent="off_topic", language="roman_urdu"), [])
    assert ran == [] and seen == []
    assert response["answer"].startswith("Maaf kijiye, main sirf Cheezious")


def test_off_topic_plan_with_a_food_cue_fails_open_to_the_agent(monkeypatch):
    response, ran, seen = _run(monkeypatch, "mera pizza order kahan hai", Plan(intent="off_topic"),
                               ["Please log in to see your orders."])
    assert len(seen) == 1 and seen[0][1]  # the agent ran, with tools
    assert response["answer"] == "Please log in to see your orders."


def test_plan_runs_the_tool_and_the_model_only_words_the_reply(monkeypatch):
    found = {"items": [{"item_id": "x", "name": "Bazooka", "type": "burger", "price": 630, "available": True}]}
    response, ran, seen = _run(monkeypatch, "kuch spicy", Plan(intent="recommend", wish="spicy"),
                               ["Try our Bazooka burger, Rs 630."], {"search_menu": found})
    assert ran == [("search_menu", {"query": "spicy"})]
    assert len(seen) == 1 and seen[0][1] is False  # no tool schemas: the lookup is already done
    assert response["answer"] == "Try our Bazooka burger, Rs 630." and response["items"][0]["name"] == "Bazooka"


def test_craving_with_no_match_also_fetches_best_sellers(monkeypatch):
    best = {"items": [{"item_id": "y", "name": "Crown Crust", "type": "pizza", "price": 1350, "available": True}]}
    response, ran, seen = _run(monkeypatch, "kuch khatta ha?", Plan(intent="recommend", wish="sour"),
                               ["Khatta kuch nahi hai, lekin Crown Crust pizza try karein, Rs 1350."],
                               {"search_menu": {"items": [], "note": "no_match"}, "get_best_sellers": best})
    assert [r[0] for r in ran] == ["search_menu", "get_best_sellers"]
    note = [m for m in seen[0][0] if isinstance(m, ToolMessage) and m.name == "get_best_sellers"][0].content
    assert "Nothing on our menu matches" in note and "sour" in note
    assert response["answer"].startswith('Abhi menu mein "sour" jaisa kuch nahi hai')  # said by code, not the model
    assert "Crown Crust" in response["answer"]


def test_no_plan_falls_back_to_the_word_list_path(monkeypatch):
    """The planner down (None): the old path still answers; a clear cart question is still pre-run."""
    response, ran, seen = _run(monkeypatch, "What's in my cart?", None, ["Your cart is empty."])
    assert [r[0] for r in ran] == ["view_cart"] and response["answer"] == "Your cart is empty."


def test_trace_records_the_plan_and_the_model(monkeypatch):
    traces = []
    response, _, _ = _run(monkeypatch, "is the branch open", Plan(intent="branch"), ["Yes, the I-8 branch is open."],
                          {"check_branch_status": {"open_now": True, "branch": "I-8"}}, traces=traces)
    assert response["debug"]["tools"] == ["check_branch_status"]
    assert traces[0]["model"] == g._model_name()
    assert {"plan": {"intent": "branch", "items": [], "language": "english"}} in traces[0]["steps"]


def test_looking_at_one_listed_item_keeps_the_list(monkeypatch):
    """burgers dikhao -> 3 cards; "pehli wali kitne ki hai" -> 1 card; "dusri wali" must still mean the 2nd burger."""
    burgers = [{"item_id": f"b{n}", "name": name, "type": "burger", "price": p, "available": True}
               for n, (name, p) in enumerate([("Reggy Burger", 390), ("Bazinga Burger", 560), ("Bazooka", 630)], 1)]
    monkeypatch.setattr(g.repo, "get_branch_status", lambda b: {"branch": "I-8", "city": "Islamabad"})
    monkeypatch.setattr(g, "price_cart", lambda lines, b: {"lines": [], "item_count": 0, "subtotal": 0})
    monkeypatch.setattr(g, "_trace", lambda r: None)
    plans = iter([Plan(intent="browse", wish="burger"), Plan(intent="item_info")])
    monkeypatch.setattr(g, "_plan_message", lambda prompt: next(plans))
    again = {**burgers[0], "item_id": "b9"}  # the menu lists Reggy Burger twice, under different ids
    results = iter([{"items": burgers}, {"match": "exact", "items": [again]}])
    monkeypatch.setattr(g, "run_tool_with_cart", lambda ctx, name, raw, cart_lines: (next(results), cart_lines))

    def fake_llm(msgs, allow_tools):
        asked = [m for m in msgs if isinstance(m, g.HumanMessage)][-1].content
        return AIMessage(content="Reggy Burger Rs 390 ki hai." if "pehli" in asked else
                         "We have Reggy Burger Rs 390, Bazinga Burger Rs 560 and Bazooka burger Rs 630.")

    monkeypatch.setattr(g, "_invoke_llm", fake_llm)
    graph = g.build_graph(InMemorySaver())
    cfg = {"configurable": {"thread_id": "t", "user_id": None, "branch_id": "b1"}}
    graph.invoke({"messages": [g.HumanMessage(content="burgers dikhao")], "cart": []}, config=cfg)
    out = graph.invoke({"messages": [g.HumanMessage(content="pehli wali kitne ki hai")]}, config=cfg)
    assert [i["name"] for i in out["response"]["items"]] == ["Reggy Burger"]
    assert [o["name"] for o in graph.get_state(cfg).values["offered"]] == ["Reggy Burger", "Bazinga Burger", "Bazooka"]


def test_named_item_we_dont_have_is_said_by_code(monkeypatch):
    near = {"items": [{"item_id": "x", "name": "Bazooka", "type": "burger", "price": 630, "available": True}]}
    monkeypatch.setattr(g, "index", lambda: type("I", (), {"corrections": lambda self, q: {}})())
    response, ran, seen = _run(monkeypatch, "do you have sushi?", Plan(intent="item_info", items=[PlanItem(name="sushi")]),
                               ["You might like our Bazooka burger, Rs 630."], {"search_menu": near})
    note = [m for m in seen[0][0] if isinstance(m, ToolMessage)][0].content
    assert json.loads(note)["note"].startswith('We don\'t have "sushi"')
    assert response["answer"].startswith("Sorry, we don't have sushi on the menu.")


def test_retry_after_prefetch_does_not_ask_for_tools(monkeypatch):
    found = {"items": [{"item_id": "x", "name": "Bazooka", "type": "burger", "price": 630, "available": True}]}
    response, ran, seen = _run(monkeypatch, "bazooka price", Plan(intent="item_info", items=[PlanItem(name="bazooka")]),
                               ["Bazooka is Rs 999.", "Bazooka is Rs 630."], {"search_menu": found})
    feedback = seen[1][0][-1].content
    assert "don't call tools" in feedback and "Call the right tool" not in feedback
    assert response["answer"] == "Bazooka is Rs 630."


def test_group_meal_shows_deals_without_a_nothing_matches_line(monkeypatch):
    deals = {"deals": [{"item_id": "d1", "name": "Somewhat Amazing 4", "type": "deal", "price": 2150, "available": True}]}
    response, ran, _ = _run(monkeypatch, "4 log hain kya order karein", Plan(intent="recommend", wish="deal"),
                            ["Somewhat Amazing 4 deal Rs 2150 aap ke liye acha rahega."], {"get_deals": deals})
    assert [r[0] for r in ran] == ["get_deals"]  # deals found: no best-seller fallback
    assert response["answer"].startswith("Somewhat Amazing 4")


def test_code_does_not_repeat_what_the_model_already_said(monkeypatch):
    near = {"items": [{"item_id": "x", "name": "Bazooka", "type": "burger", "price": 630, "available": True}]}
    monkeypatch.setattr(g, "index", lambda: type("I", (), {"corrections": lambda self, q: {}})())
    response, _, _ = _run(monkeypatch, "do you have sushi?", Plan(intent="item_info", items=[PlanItem(name="sushi")]),
                          ["Sorry, we don't have sushi. You might like our Bazooka burger, Rs 630."], {"search_menu": near})
    assert response["answer"] == "Sorry, we don't have sushi. You might like our Bazooka burger, Rs 630."
