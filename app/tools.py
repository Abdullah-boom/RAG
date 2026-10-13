"""Tools the agent can call (spec 9). Plain functions + Pydantic arg models; framework-independent.

Security rule: user_id and branch_id come from RequestContext (server side), never from LLM arguments.
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Type

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

from app import cart
from app.data import repositories as repo
from app.retrieval import index

MAX_RESULT_CHARS = 6000


@dataclass(frozen=True)
class RequestContext:
    user_id: Optional[str]  # from verified auth (here: the CLI's --user); None = guest
    branch_id: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------- argument models ----------
# extra="forbid": a misspelled argument must be an error the model can fix, never silently dropped.

class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchMenuArgs(Args):
    query: str = Field(..., max_length=200,
                       description="What the customer wants, rewritten as short ENGLISH keywords "
                                   "(translate Urdu / Roman Urdu), e.g. 'spicy chicken pizza'.")
    max_price: Optional[float] = Field(None, gt=0, description="Maximum price in PKR, if the customer gave a budget.")
    deals_only: bool = Field(False, description="True to search only deals / combos.")
    limit: int = Field(5, ge=1, le=5)


class ItemArgs(Args):
    item_id: str = Field(..., max_length=40, description="item_id from a previous search_menu / get_deals result.")


class DealsArgs(Args):
    limit: int = Field(8, ge=1, le=10)


class BestSellersArgs(Args):
    limit: int = Field(5, ge=1, le=8)


class NoArgs(Args):
    pass


class OrderStatusArgs(Args):
    order_no: Optional[str] = Field(None, max_length=40,
                                    description="Order number like '170225-13665'. Omit for the latest order.")


class OrderHistoryArgs(Args):
    limit: int = Field(5, ge=1, le=10)


class FaqArgs(Args):
    query: str = Field(..., max_length=200, description="The question, in English keywords.")


class LineItem(Args):
    item_id: str = Field(..., max_length=40)
    qty: int = Field(1, ge=1, le=50, validation_alias=AliasChoices("qty", "quantity"))
    size: Optional[str] = Field(None, max_length=40, description="Size name exactly as returned, e.g. 'Regular'.")


class CalculateArgs(Args):
    items: List[LineItem] = Field(..., min_length=1, max_length=15)


# ---------- implementations ----------

def search_menu(ctx: RequestContext, a: SearchMenuArgs) -> dict:
    hits = index().search(a.query, "menu", k=8, branch_id=ctx.branch_id,
                          max_price=a.max_price, deals_only=a.deals_only)
    items = repo.get_items([h["item_id"] for h in hits], ctx.branch_id)
    if a.max_price is not None:  # coarse index filter -> exact check on live price
        items = [i for i in items if (i.get("price_from") or i["price"] or 0) and (i.get("price_from") or i["price"]) <= a.max_price]
    items = [i for i in items if i["available"]][: a.limit]
    return {"as_of": utc_now_iso(), "items": items} if items else {"as_of": utc_now_iso(), "items": [], "note": "no_match"}


def get_item_details(ctx: RequestContext, a: ItemArgs) -> dict:
    items = repo.get_items([a.item_id], ctx.branch_id, detailed=True)
    return {"as_of": utc_now_iso(), "item": items[0]} if items else {"error": "not_found"}


def get_deals(ctx: RequestContext, a: DealsArgs) -> dict:
    deals = [i for i in repo.get_items(repo.get_deal_ids(ctx.branch_id), ctx.branch_id) if i["available"]]
    return {"as_of": utc_now_iso(), "deals": deals[: a.limit], "total_deals": len(deals),
            "active_discounts": repo.get_active_discounts(ctx.branch_id)}


def get_best_sellers(ctx: RequestContext, a: BestSellersArgs) -> dict:
    items = [i for i in repo.get_items(repo.get_best_seller_ids(ctx.branch_id), ctx.branch_id) if i["available"]]
    return {"as_of": utc_now_iso(), "source": "items marked 'best seller' by Cheezious", "items": items[: a.limit]}


def check_branch_status(ctx: RequestContext, a: NoArgs) -> dict:
    s = repo.get_branch_status(ctx.branch_id)
    return {"as_of": utc_now_iso(), **s} if s else {"error": "branch_not_found"}


def get_order_status(ctx: RequestContext, a: OrderStatusArgs) -> dict:
    if not ctx.user_id:
        return {"error": "not_logged_in"}
    o = repo.get_order(ctx.user_id, a.order_no)
    return {"as_of": utc_now_iso(), "order": o} if o else {"error": "not_found"}


def get_order_history(ctx: RequestContext, a: OrderHistoryArgs) -> dict:
    if not ctx.user_id:
        return {"error": "not_logged_in"}
    return {"as_of": utc_now_iso(), "orders": repo.get_order_history(ctx.user_id, a.limit)}


def search_faq(ctx: RequestContext, a: FaqArgs) -> dict:
    hits = index().search(a.query, "faq", k=3)
    return {"as_of": utc_now_iso(),
            "faqs": [{"question": h["question"], "answer": h["answer"], "source": h["source"]} for h in hits]}


def calculate_total(ctx: RequestContext, a: CalculateArgs) -> dict:
    live = {i["item_id"]: i for i in repo.get_items([li.item_id for li in a.items], ctx.branch_id)}
    lines, subtotal = [], 0
    for li in a.items:
        item = live.get(li.item_id)
        if not item or not item["available"]:
            return {"error": "item_unavailable", "item_id": li.item_id}
        unit = item["price"]
        if item.get("sizes") and not li.size:
            return {"error": "size_required", "item_id": li.item_id, "valid_sizes": [s["size"] for s in item["sizes"]]}
        if li.size:
            match = [s for s in item.get("sizes", []) if s["size"].lower() == li.size.strip().lower()]
            if not match:
                return {"error": "unknown_size", "item_id": li.item_id,
                        "valid_sizes": [s["size"] for s in item.get("sizes", [])]}
            unit = match[0]["price"]
        line_total = unit * li.qty
        subtotal += line_total
        lines.append({"name": item["name"], "size": li.size, "qty": li.qty, "unit_price": unit, "line_total": line_total})
    return {"as_of": utc_now_iso(), "lines": lines, "subtotal": subtotal, "currency": repo.CURRENCY,
            "note": "Menu prices before tax, delivery fee and discounts; the app shows the final total at checkout."}


@dataclass(frozen=True)
class Tool:
    name: str
    fn: Callable  # (ctx, args) -> dict, or with uses_cart: (ctx, args, cart) -> (dict, new_cart)
    args: Type[BaseModel]
    description: str
    uses_cart: bool = False


TOOLS: Dict[str, Tool] = {t.name: t for t in [
    Tool("search_menu", search_menu, SearchMenuArgs,
         "Find menu items or deals matching a request (name, taste, ingredients, budget). "
         "Returns live prices and availability at the customer's branch."),
    Tool("get_item_details", get_item_details, ItemArgs,
         "Full details for one item: description, all sizes with prices, choices/flavours, availability."),
    Tool("get_deals", get_deals, DealsArgs,
         "List current deals/combos at the customer's branch, plus any active discounts."),
    Tool("get_best_sellers", get_best_sellers, BestSellersArgs,
         "Most popular / best-selling items at the customer's branch (e.g. 'what's popular', "
         "'sab se zyada bikne wala'). Returns live prices."),
    Tool("check_branch_status", check_branch_status, NoArgs,
         "Is the customer's branch open now, today's hours, services (delivery/pickup), minimum order."),
    Tool("get_order_status", get_order_status, OrderStatusArgs,
         "Status, items and total of one of the logged-in customer's own orders (latest if no order number)."),
    Tool("get_order_history", get_order_history, OrderHistoryArgs,
         "The logged-in customer's recent orders."),
    Tool("search_faq", search_faq, FaqArgs, "Search published customer FAQs / app help."),
    Tool("calculate_total", calculate_total, CalculateArgs,
         "Price a hypothetical set of items without changing the cart (e.g. 'how much would 2 X cost?')."),
    Tool("add_to_cart", cart.add_to_cart, cart.AddToCartArgs,
         "Add one or more items to the customer's cart by menu name (or item_id), with size and qty. "
         "Returns the updated cart with live prices and subtotal.", uses_cart=True),
    Tool("remove_from_cart", cart.remove_from_cart, cart.RemoveFromCartArgs,
         "Remove an item (or some of its quantity) from the cart, by line number or name.", uses_cart=True),
    Tool("view_cart", cart.view_cart, cart.EmptyArgs,
         "Show the cart with live prices and subtotal.", uses_cart=True),
    Tool("clear_cart", cart.clear_cart, cart.EmptyArgs, "Empty the cart.", uses_cart=True),
]}


def _clean_schema(schema: dict) -> dict:
    schema = json.loads(json.dumps(schema))
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    return schema


def tool_schemas() -> List[dict]:
    return [{"type": "function", "function": {
        "name": t.name, "description": t.description, "parameters": _clean_schema(t.args.model_json_schema())}}
        for t in TOOLS.values()]


SECURITY_ARG = re.compile(r"(user|customer|branch)_?id", re.I)


def run_tool(ctx: RequestContext, name: str, raw_args: str) -> dict:
    """Validate and execute one tool call (no cart). Never raises; errors come back as {'error': ...}."""
    return run_tool_with_cart(ctx, name, raw_args, [])[0]


def run_tool_with_cart(ctx: RequestContext, name: str, raw_args: str, cart_lines: List[dict]):
    """Like run_tool, but cart tools may change the cart. Returns (result, cart)."""
    tool = TOOLS.get(name)
    if not tool:
        return {"error": "unknown_tool", "available": list(TOOLS)}, cart_lines
    try:
        payload = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        return {"error": "invalid_json_arguments"}, cart_lines
    if not isinstance(payload, dict):
        return {"error": "invalid_arguments"}, cart_lines
    # Identity can't be passed in: drop any security-relevant key the model invents.
    payload = {k: v for k, v in payload.items() if not SECURITY_ARG.fullmatch(k)}
    try:
        args = tool.args.model_validate(payload)
    except ValidationError as e:
        return {"error": "invalid_arguments", "details": e.errors(include_url=False, include_input=False)[:3]}, cart_lines
    try:
        if tool.uses_cart:
            result, cart_lines = tool.fn(ctx, args, cart_lines)
        else:
            result = tool.fn(ctx, args)
    except Exception:  # noqa: BLE001 - never leak stack traces to the model
        return {"error": "temporarily_unavailable"}, cart_lines
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        return {"error": "result_too_large", "hint": "narrow the query or lower limit"}, cart_lines
    return {"as_of": utc_now_iso(), **result} if tool.uses_cart else result, cart_lines
