"""Cart logic against the dev DB (read-only, no LLM)."""
import json

from app.tools import RequestContext, run_tool_with_cart

CTX = RequestContext(None, "66b38e6f50d929a6d91472d4")  # Gulberg Lahore


def call(name, args, cart):
    return run_tool_with_cart(CTX, name, json.dumps(args), cart)


def test_add_by_name_with_size_and_live_subtotal():
    r, cart = call("add_to_cart", {"items": [{"name": "chicken tikka", "size": "regular", "qty": 2},
                                             {"name": "bazooka", "qty": 1}]}, [])
    assert not r.get("problems"), r
    lines = r["cart"]["lines"]
    assert [l["qty"] for l in lines] == [2, 1]
    assert r["cart"]["subtotal"] == sum(l["line_total"] for l in lines)
    assert lines[0]["size"] == "Regular" and lines[0]["line_total"] == 2 * lines[0]["unit_price"]


def test_size_required():
    r, cart = call("add_to_cart", {"items": [{"name": "chicken tikka", "qty": 1}]}, [])
    assert cart == [] and r["problems"][0]["error"] == "size_required"


def test_unknown_item_is_not_added():
    r, cart = call("add_to_cart", {"items": [{"name": "laptop", "qty": 1}]}, [])
    assert cart == [] and r["problems"][0]["error"] in ("not_found", "no_exact_match")


def test_adding_same_item_merges_and_remove_decrements():
    _, cart = call("add_to_cart", {"items": [{"name": "bazooka", "qty": 1}]}, [])
    _, cart = call("add_to_cart", {"items": [{"name": "bazooka", "qty": 2}]}, cart)
    assert len(cart) == 1 and cart[0]["qty"] == 3
    r, cart = call("remove_from_cart", {"name": "bazooka", "qty": 1}, cart)
    assert cart[0]["qty"] == 2
    r, cart = call("remove_from_cart", {"line_no": 1}, cart)
    assert cart == [] and r["cart"]["subtotal"] == 0


def test_cart_stores_no_prices():
    _, cart = call("add_to_cart", {"items": [{"name": "bazooka", "qty": 1}]}, [])
    assert set(cart[0]) == {"item_id", "name", "size", "qty"}  # prices always re-read live


def test_clear():
    _, cart = call("add_to_cart", {"items": [{"name": "bazooka", "qty": 1}]}, [])
    r, cart = call("clear_cart", {}, cart)
    assert cart == [] and r["cleared"]


def test_qty_is_required():
    r, cart = call("add_to_cart", {"items": [{"name": "bazooka"}]}, [])
    assert r["error"] == "invalid_arguments" and cart == []
