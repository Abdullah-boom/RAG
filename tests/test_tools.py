import pytest
"""Tool behaviour against the dev DB (read-only)."""
from app.tools import RequestContext, run_tool

CTX = RequestContext(None, "66b38e6f50d929a6d91472d4")  # Gulberg Lahore


def test_search_returns_live_prices():
    r = run_tool(CTX, "search_menu", '{"query": "chicken tikka"}')
    assert r["items"] and all(i["available"] for i in r["items"])
    assert all(i["price"] is not None or i.get("sizes") for i in r["items"])


def test_budget_filter_is_exact():
    r = run_tool(CTX, "search_menu", '{"query": "spicy", "max_price": 700}')
    assert all((i.get("price_from") or i["price"]) <= 700 for i in r["items"])


def test_nonsense_query_returns_nothing():
    assert run_tool(CTX, "search_menu", '{"query": "laptop repair"}')["items"] == []


def test_calculator_uses_db_prices_and_requires_size():
    item = run_tool(CTX, "search_menu", '{"query": "chicken tikka"}')["items"][0]
    assert run_tool(CTX, "calculate_total", f'{{"items": [{{"item_id": "{item["item_id"]}", "qty": 2}}]}}')["error"] == "size_required"
    size = item["sizes"][0]
    r = run_tool(CTX, "calculate_total",
                 f'{{"items": [{{"item_id": "{item["item_id"]}", "qty": 2, "size": "{size["size"]}"}}]}}')
    assert r["subtotal"] == 2 * size["price"]


def test_invalid_args_return_structured_error():
    assert run_tool(CTX, "search_menu", '{"query": "x", "limit": 99}')["error"] == "invalid_arguments"
    assert run_tool(CTX, "nope", "{}")["error"] == "unknown_tool"
    assert run_tool(CTX, "get_item_details", '{"item_id": "not-an-id"}') == {"error": "not_found"}


def test_exact_name_ranks_first():
    r = run_tool(CTX, "search_menu", '{"query": "chicken tikka pizza"}')
    assert r["items"][0]["name"] == "Chicken Tikka"


def test_unknown_argument_is_an_error_not_ignored():
    assert run_tool(CTX, "search_menu", '{"query": "pizza", "colour": "red"}')["error"] == "invalid_arguments"


def test_quantity_alias():
    item = run_tool(CTX, "search_menu", '{"query": "chicken tikka"}')["items"][0]
    size = item["sizes"][0]
    r = run_tool(CTX, "calculate_total",
                 f'{{"items": [{{"item_id": "{item["item_id"]}", "quantity": 3, "size": "{size["size"]}"}}]}}')
    assert r["subtotal"] == 3 * size["price"]


def test_best_sellers_are_sold_at_branch_with_live_prices():
    r = run_tool(CTX, "get_best_sellers", "{}")
    assert r["items"] and all(i["available"] and i["sold_at_this_branch"] for i in r["items"])


# ---------- ambiguous names (spec rule 8): offline, with a fake index + live-facts stub ----------

class _FakeIdx:
    DOCS = [  # name, category/description words the keyword pass sees
        ("1", "Chicken Tikka", "Item: Chicken Tikka Category: Somewhat Local Description: grilled chicken"),
        ("2", "Black Pepper Tikka", "Item: Black Pepper Tikka Category: Somewhat Sooper Description: pepper chicken"),
        ("3", "Malai Tikka", "Item: Malai Tikka Category: Special Pizza Description: malai tikka chunks"),
        ("4", "Chicken Mushroom", "Item: Chicken Mushroom Category: Somewhat Sooper Description: chicken tikka, mushrooms"),
        ("5", "Chicken Fajita", "Item: Chicken Fajita Category: Somewhat Local Description: fajita chicken"),
    ]

    def __init__(self):
        from app.retrieval import Index
        self.docs = [{"text": t, "metadata": {"item_id": i, "name": n, "kind": "menu", "branch_ids": ["b"]}}
                     for i, n, t in self.DOCS]
        self.keyword_matches = Index.keyword_matches.__get__(self)
        self.correct = lambda q: q

    def search(self, query, kind, **k):  # ranked search: like the real one, misses some tikka items
        return [{"item_id": i} for i in ("1", "5", "4")]


@pytest.fixture
def menu(monkeypatch):
    import app.tools as t
    monkeypatch.setattr(t, "index", lambda: _FakeIdx())
    names = {i: n for i, n, _ in _FakeIdx.DOCS}
    monkeypatch.setattr(t.repo, "get_items", lambda ids, b, detailed=False: [
        {"item_id": i, "name": names[i], "price": 690, "available": True} for i in ids])
    ctx = t.RequestContext(user_id=None, branch_id="b")
    return lambda q, **kw: t.search_menu(ctx, t.SearchMenuArgs(query=q, **kw))


def test_ingredient_word_lists_all_options(menu):
    r = menu("chicken tikka")
    assert r["match"] == "several"
    assert [i["name"] for i in r["items"]][0] == "Chicken Tikka"  # exact name first
    assert {i["name"] for i in r["items"]} == {"Chicken Tikka", "Black Pepper Tikka", "Malai Tikka", "Chicken Mushroom"}


def test_picked_returns_only_that_item(menu):
    r = menu("chicken tikka pizza", picked=True)
    assert r["match"] == "exact" and [i["name"] for i in r["items"]] == ["Chicken Tikka"]


def test_unique_full_name_answers_directly(menu):
    r = menu("black pepper tikka")
    assert r["match"] == "exact" and [i["name"] for i in r["items"]] == ["Black Pepper Tikka"]


def test_generic_words_alone_do_not_trigger_options(menu):
    r = menu("chicken")
    assert r.get("match") != "several" and r["items"]


def test_customer_typo_is_reported_not_hidden(menu, monkeypatch):
    import app.tools as t
    idx = _FakeIdx()
    idx.corrections = lambda q: {"tiqqa": "tikka"} if "tiqqa" in q else {}
    monkeypatch.setattr(t, "index", lambda: idx)
    ctx = t.RequestContext(None, "b", user_message="how much is chicken tiqqa")
    r = t.search_menu(ctx, t.SearchMenuArgs(query="chicken tikka"))
    assert r["exact"] is False and r["spelling_assumed"] == {"tiqqa": "tikka"}
    # the customer's own words name the item: no uncertainty flag
    ctx = t.RequestContext(None, "b", user_message="black pepper tikka price")
    assert "exact" not in t.search_menu(ctx, t.SearchMenuArgs(query="black pepper tikka"))


def test_item_type_from_category():
    from app.data.repositories import item_type
    assert item_type(["Somewhat Local"], False) == "pizza"
    assert item_type(["Cheezy Treats"], False) == "pizza"  # Behari Kebab is a pizza
    assert item_type(["Burgerz"], False) == "burger"
    assert item_type(["Unknown range"], True) == "deal"
    assert item_type(["Unknown range"], False) is None


def test_duplicate_products_with_same_prices_are_merged():
    from app.data.repositories import merge_duplicates
    a = {"item_id": "old", "name": "Chicken Tikka", "price": None, "sizes": [{"size": "Small", "price": 660}], "available": True}
    b = {**a, "item_id": "new"}
    c = {**a, "item_id": "pricier", "sizes": [{"size": "Small", "price": 690}]}  # different price: a data problem, kept
    assert [i["item_id"] for i in merge_duplicates([a, b, c])] == ["old", "pricier"]


def test_budget_or_description_searches_get_no_exactness_warning(menu, monkeypatch):
    import app.tools as t
    idx = _FakeIdx()
    idx.corrections = lambda q: {}
    monkeypatch.setattr(t, "index", lambda: idx)
    ctx = t.RequestContext(None, "b", user_message="koi deal hai 1500 se kam?")
    assert "exact" not in t.search_menu(ctx, t.SearchMenuArgs(query="deal", max_price=1500))
    ctx = t.RequestContext(None, "b", user_message="something with chicken")
    assert "exact" not in t.search_menu(ctx, t.SearchMenuArgs(query="chicken"))


def test_category_query_lists_every_item_of_that_kind(monkeypatch):
    """'burger' and 'burgers' must give the same full list (similarity search used to drop some)."""
    import app.tools as t

    def doc(item_id, name, cats, branches=("b1",)):
        return {"metadata": {"kind": "menu", "item_id": item_id, "name": name, "categories": cats,
                             "is_deal": False, "branch_ids": list(branches)}}

    class Idx:
        docs = [doc("1", "Reggy Burger", ["Burgerz"]), doc("2", "Bazooka", ["Burgerz"]),
                doc("3", "Bazinga Burger", ["Burgerz"]), doc("4", "Crown Crust", ["Special Pizza"]),
                doc("5", "Zinger Max", ["Burgerz"], branches=("other",))]

    live = {"1": 390, "2": 630, "3": 560, "4": 1480}
    monkeypatch.setattr(t, "index", lambda: Idx())
    monkeypatch.setattr(t.repo, "get_items", lambda ids, b, detailed=False: [
        {"item_id": i, "name": next(d["metadata"]["name"] for d in Idx.docs if d["metadata"]["item_id"] == i),
         "type": "burger" if i != "4" else "pizza", "price": live[i], "available": True} for i in ids if i in live])
    for q in ("burger", "Burgers"):
        r = t.search_menu(t.RequestContext(None, "b1", user_message=q), t.SearchMenuArgs(query=q))
        assert r["match"] == "category" and r["count"] == 3
        assert [i["name"] for i in r["items"]] == ["Reggy Burger", "Bazinga Burger", "Bazooka"]  # cheapest first


@pytest.mark.parametrize("said,is_wish", [("i want something cheesey", True), ("kuch teekha chahiye", True),
                                          ("anything light?", True), ("tiqqa pizza", False), ("zinger max", False)])
def test_descriptions_are_wishes_not_names(said, is_wish):
    """'something cheesey' gets suggestions; only item names get "couldn't find exactly"."""
    import app.tools as t
    assert bool(t.DESCRIPTION.search(said)) is is_wish
