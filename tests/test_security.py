"""User A must never read user B's data, whatever the model sends (spec 13). Runs against the dev DB."""
import pytest

from app.data.db import order_db
from app.tools import RequestContext, run_tool

BRANCH = "66b38e6f50d929a6d91472d4"


@pytest.fixture(scope="module")
def two_users():
    ids = [r["_id"] for r in order_db().orders.aggregate(
        [{"$group": {"_id": "$customerId", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}, {"$limit": 2}])]
    a, b = ids
    b_order = order_db().orders.find_one({"customerId": b}, {"orderNo": 1})["orderNo"]
    a_orders = set(order_db().orders.distinct("orderNo", {"customerId": a}))
    return a, b, b_order, a_orders


def test_cannot_read_other_users_order_by_number(two_users):
    a, _, b_order, _ = two_users
    r = run_tool(RequestContext(a, BRANCH), "get_order_status", f'{{"order_no": "{b_order}"}}')
    assert r == {"error": "not_found"}


@pytest.mark.parametrize("key", ["user_id", "customer_id", "customerId", "userId"])
def test_injected_identity_args_are_ignored(two_users, key):
    a, b, _, a_orders = two_users
    r = run_tool(RequestContext(a, BRANCH), "get_order_history", f'{{"{key}": "{b}", "limit": 10}}')
    assert {o["order_no"] for o in r["orders"]} <= a_orders


def test_branch_cannot_be_overridden(two_users):
    r = run_tool(RequestContext(None, BRANCH), "check_branch_status", '{"branch_id": "6509816257211fb674f19bcc"}')
    assert r["branch_id"] == BRANCH


def test_guest_gets_no_orders():
    assert run_tool(RequestContext(None, BRANCH), "get_order_status", "{}") == {"error": "not_logged_in"}
    assert run_tool(RequestContext(None, BRANCH), "get_order_history", "{}") == {"error": "not_logged_in"}


def test_order_results_contain_no_contact_details(two_users):
    a = two_users[0]
    text = str(run_tool(RequestContext(a, BRANCH), "get_order_status", "{}"))
    for field in ("customerPhoneNo", "customerName", "address", "lat", "riderId"):
        assert field not in text
