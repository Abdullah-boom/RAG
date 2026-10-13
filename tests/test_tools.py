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
