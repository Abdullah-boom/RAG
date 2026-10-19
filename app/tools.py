"""Tools the agent can call (spec 9). Plain functions + Pydantic arg models; framework-independent.

Security rule: user_id and branch_id come from RequestContext (server side), never from LLM arguments.
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Type

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

from pymongo.errors import PyMongoError

from app import cart, checkout, payments
from app.data import repositories as repo
from app.data.db import with_retry
from app.retrieval import GENERIC_WORDS, index

MAX_RESULT_CHARS = 6000
DB_UNAVAILABLE = "database_unavailable"  # the graph ends the turn with a clear error when a tool returns this


@dataclass(frozen=True)
class RequestContext:
    user_id: Optional[str]  # from verified auth (here: the CLI's --user); None = guest
    branch_id: str
    user_message: Optional[str] = None  # the customer's own words this turn (the model rewrites queries)
    payment_method: Optional[str] = None  # the method already chosen for this branch's cart, if any
    offered: tuple = ()  # items shown to the customer last turn, in order ({item_id, name, type}): "the first one"


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
    picked: bool = Field(False, description="True ONLY when the customer has clearly chosen one specific item "
                         "by its exact menu name: replying to options you listed, or naming it with its type "
                         "('the Chicken Tikka pizza'). A bare dish or ingredient word ('chicken tikka', 'tikka') "
                         "is NOT a pick.")
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

MAX_OPTIONS = 6
MAX_CATEGORY_ITEMS = 10
# A query that is just a kind of item lists that whole kind (app/ingestion/item_types.yaml), not the top few
# by similarity, which used to drop items ("burger" missed Reggy Burger, "burgers" missed Bazooka).
CATEGORY_WORDS = cart.CATEGORY_WORDS


def _name_key(name: str) -> str:
    return " ".join(re.findall(r"\w+", name.lower()))


def _is_exact(name: str, query_key: str) -> bool:
    """The query names this item: all its name words, plus at most generic words ("the ... pizza", "price")."""
    name_words, query_words = set(_name_key(name).split()), set(query_key.split())
    return bool(name_words) and name_words <= query_words and query_words - name_words <= GENERIC_WORDS


def _category_items(idx, kind: str, branch_id: str, max_price: Optional[float]) -> dict:
    """Every available item of one kind at this branch, cheapest first."""
    ids = [d["metadata"]["item_id"] for d in idx.docs if d["metadata"]["kind"] == "menu"
           and branch_id in (d["metadata"].get("branch_ids") or [])
           and repo.item_type(d["metadata"].get("categories") or [], d["metadata"].get("is_deal")) == kind]
    items = [i for i in repo.get_items(ids, branch_id) if i["available"] and i.get("type") == kind]
    if max_price is not None:
        items = [i for i in items if (i.get("price_from") or i["price"] or 0) <= max_price]
    items = sorted(repo.merge_duplicates(items), key=lambda i: i.get("price_from") or i.get("price") or 0)
    return {"as_of": utc_now_iso(), "match": "category", "category": kind, "count": len(items),
            "items": items[:MAX_CATEGORY_ITEMS],
            "note": f"All {kind} items at this branch" + (f" (showing the {MAX_CATEGORY_ITEMS} cheapest)"
                                                         if len(items) > MAX_CATEGORY_ITEMS else "") + "."}


def search_menu(ctx: RequestContext, a: SearchMenuArgs) -> dict:
    idx = index()
    kind = CATEGORY_WORDS.get(_name_key(a.query))
    if kind and not a.deals_only:
        return _category_items(idx, kind, ctx.branch_id, a.max_price)
    hits = idx.search(a.query, "menu", k=8, branch_id=ctx.branch_id, max_price=a.max_price, deals_only=a.deals_only)
    # Every item the customer's words could mean (filters already narrow budget / deal searches).
    related = [] if (a.max_price is not None or a.deals_only) else idx.keyword_matches(a.query, "menu", ctx.branch_id)
    related_names = {_name_key(r["name"]) for r in related}
    query_key = _name_key(a.query)
    ids = list(dict.fromkeys([h["item_id"] for h in hits] + [r["item_id"] for r in related]))
    items = repo.get_items(ids, ctx.branch_id)
    if a.max_price is not None:  # coarse index filter -> exact check on live price
        items = [i for i in items if (i.get("price_from") or i["price"] or 0) and (i.get("price_from") or i["price"]) <= a.max_price]
    items = repo.merge_duplicates([i for i in items if i["available"]])
    if not items:
        return {"as_of": utc_now_iso(), "items": [], "note": "no_match"}

    exact = [i for i in items if _is_exact(i["name"], query_key)]
    if a.picked and exact:
        return {"as_of": utc_now_iso(), "match": "exact", "items": exact[: a.limit]}
    # Only when the customer seems to NAME an item: budget / deal / description searches aren't names.
    words_note = {} if (a.max_price is not None or a.deals_only) else _customer_words_note(ctx, idx, items)

    if ctx.user_message and DESCRIPTION.search(ctx.user_message):  # suggestions for a wish, best matches first
        return {"as_of": utc_now_iso(), "match": "suggestions", "items": items[: a.limit],
                "note": "The customer described what they feel like, not an item name: present these as "
                        "suggestions that fit, without saying you couldn't find it."}
    # Several different items fit the words: return one per name and let the customer choose (spec rule 8).
    candidates = [i for i in items if _name_key(i["name"]) in related_names]
    by_name = {}
    for i in sorted(candidates, key=lambda i: i not in exact):  # an exact-name item first
        by_name.setdefault(_name_key(i["name"]), i)
    if len(by_name) > 1:
        options = list(by_name.values())[:MAX_OPTIONS]
        return {"as_of": utc_now_iso(), "match": "several", "items": options, **words_note,
                "note": "These different menu items all match the customer's words. Do not pick one: list them "
                        "with their type (pizza, burger...) and starting price, and ask which one the customer means."}
    if exact:  # one item fits by name: return just it, so unrelated near-misses don't get mentioned
        return {"as_of": utc_now_iso(), "match": "exact", "items": exact[: a.limit], **words_note}
    return {"as_of": utc_now_iso(), "items": items[: a.limit], **words_note}


# A wish, not a name: "something cheesy", "kuch teekha", "anything light". The results are suggestions, never
# "couldn't find 'cheesey' exactly" (that framing is for item names like "tiqqa" or "Zinger Max").
DESCRIPTION = re.compile(
    r"\b(something|anything|kuch|kuchh|kch|koi|any|cheesy|cheesey|cheezy|spicy|spicey|teekha|tikha|meetha|sweet"
    r"|crispy|crunchy|saucy|creamy|juicy|loaded|light|healthy|veg|vegetarian|mild|filling|tasty|delicious|yummy"
    r"|mazedar|mazaydar|lazeez|zabardast|chatpata|mirchi|tez|karara|kurkura|smoky|spicey|tikha|halka|snack|snacks"
    r"|veggie|sabzi|no\s+meat)\b", re.I)


def _customer_words_note(ctx: RequestContext, idx, items: List[dict]) -> dict:
    """Compare the items with what the CUSTOMER typed (the model may have fixed spelling or added words).
    exact=false: no item's full name is in the customer's own words, so the bot must say it is not sure."""
    said = ctx.user_message
    if not said or DESCRIPTION.search(said):
        return {}
    said_words = set(_name_key(said).split())
    if any(set(_name_key(i["name"]).split()) <= said_words for i in items):
        return {}
    fixed = idx.corrections(said)
    distinctive = {w for w in said_words if w not in GENERIC_WORDS and len(w) > 2 and not w.isdigit()}
    name_words = {w for i in items for w in _name_key(i["name"]).split()}
    if not fixed and not distinctive & name_words:
        return {}  # a description ("something spicy"), not an attempt at an item's name
    note = {"exact": False, "customer_wrote": said[:120]}
    if fixed:
        note["spelling_assumed"] = fixed
    return note


def get_item_details(ctx: RequestContext, a: ItemArgs) -> dict:
    items = repo.get_items([a.item_id], ctx.branch_id, detailed=True)
    return {"as_of": utc_now_iso(), "item": items[0]} if items else {"error": "not_found"}


def get_deals(ctx: RequestContext, a: DealsArgs) -> dict:
    deals = repo.merge_duplicates([i for i in repo.get_items(repo.get_deal_ids(ctx.branch_id), ctx.branch_id)
                                   if i["available"]])
    return {"as_of": utc_now_iso(), "deals": deals[: a.limit], "total_deals": len(deals),
            "active_discounts": repo.get_active_discounts(ctx.branch_id)}


def get_best_sellers(ctx: RequestContext, a: BestSellersArgs) -> dict:
    items = [i for i in repo.get_items(repo.get_best_seller_ids(ctx.branch_id), ctx.branch_id) if i["available"]]
    return {"as_of": utc_now_iso(), "source": "items marked 'best seller' by Cheezious", "items": items[: a.limit]}


def check_branch_status(ctx: RequestContext, a: NoArgs) -> dict:
    s = repo.get_branch_status(ctx.branch_id)
    return {"as_of": utc_now_iso(), **s} if s else {"error": "branch_not_found"}


class PaymentArgs(Args):
    method: str = Field(..., max_length=60, description="The method the customer chose, as they said it or its code, "
                                                       "e.g. 'cash on delivery', 'online card', 'COD'.")


def get_payment_methods(ctx: RequestContext, a: NoArgs, cart_lines: List[dict]):
    """Methods this branch accepts now, each with the estimated tax and total for the current cart."""
    subtotal = cart.price_cart(cart_lines, ctx.branch_id)["subtotal"] if cart_lines else 0
    opts = payments.options(repo.get_payment_config(ctx.branch_id), subtotal)
    result = {"methods": [{k: o[k] for k in ("code", "name", "when", "pay_now", "tax_rate", "tax", "total")}
                          for o in opts],
              "cart_subtotal": subtotal, "chosen": ctx.payment_method, "note": payments.ESTIMATE_NOTE,
              "secure_note": payments.SECURE_NOTE}
    if not opts:
        result["error"] = "no_payment_methods"
    return result, cart_lines


def set_payment_method(ctx: RequestContext, a: PaymentArgs, cart_lines: List[dict]):
    """Record the customer's choice for this branch's cart. Nothing is charged: the app's checkout does that."""
    config = repo.get_payment_config(ctx.branch_id)
    chosen = payments.find(config, a.method)
    if not chosen:
        several = payments.candidates(config, a.method)
        if len(several) > 1:  # "card": at the door or online? Ask, don't guess.
            return {"error": "ambiguous_method", "requested": a.method,
                    "ask_which": [{"code": o["code"], "name": o["name"]} for o in several]}, cart_lines
        return {"error": "unknown_or_unavailable_method", "requested": a.method,
                "available": [{"code": o["code"], "name": o["name"]} for o in payments.options(config)]}, cart_lines
    subtotal = cart.price_cart(cart_lines, ctx.branch_id)["subtotal"] if cart_lines else 0
    est = payments.tax_estimate(subtotal, chosen["tax_rate"], config["tax_exclusive"])
    return {"payment_method_selected": chosen["code"], "name": chosen["name"], "pay_now": chosen["pay_now"],
            "cart_subtotal": subtotal, **est, "note": payments.ESTIMATE_NOTE,
            "secure_note": payments.SECURE_NOTE}, cart_lines


def review_order(ctx: RequestContext, a: NoArgs, cart_lines: List[dict]):
    """Is the cart ready to order, and what's missing? The bot can't place it: the customer taps Confirm."""
    result = checkout.review(user_id=ctx.user_id, branch_status=repo.get_branch_status(ctx.branch_id),
                             cart=cart.price_cart(cart_lines, ctx.branch_id) if cart_lines else None,
                             payment_config=repo.get_payment_config(ctx.branch_id), payment_code=ctx.payment_method)
    result["next_step"] = ("Ask the customer to tap 'Confirm order' in the cart to place it." if result["ready"]
                           else "Explain each problem and how to fix it.")
    return result, cart_lines


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
    Tool("get_payment_methods", get_payment_methods, NoArgs,
         "Payment methods this branch accepts (cash/card on delivery, online card/wallets), each with estimated "
         "tax and total for the current cart. Tax differs by method.", uses_cart=True),
    Tool("review_order", review_order, NoArgs,
         "When the customer wants to place / confirm the order: checks the cart is ready (items, payment method, "
         "branch open, minimum order, login) and returns the summary. You cannot place orders: if ready, the "
         "customer taps 'Confirm order'.", uses_cart=True),
    Tool("set_payment_method", set_payment_method, PaymentArgs,
         "Record how the customer wants to pay for this cart (they pay later in the app's checkout; nothing is "
         "charged here).", uses_cart=True),
]}


def _clean_schema(schema: dict) -> dict:
    schema = json.loads(json.dumps(schema))
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    return schema


def tool_schemas(names: Optional[List[str]] = None) -> List[dict]:
    """All tools, or only `names` (the tools this message's topics need: fewer tokens, fewer wrong picks)."""
    return [{"type": "function", "function": {
        "name": t.name, "description": t.description, "parameters": _clean_schema(t.args.model_json_schema())}}
        for t in TOOLS.values() if names is None or t.name in names]


SECURITY_ARG = re.compile(r"(user|customer|branch)_?id", re.I)


NULLISH = {"", "none", "null", "nil", "undefined"}


def _lenient(model: Type[BaseModel], payload: dict) -> dict:
    """Small models (Llama 3.1 8B) fill every optional argument: "None" for not given, a limit above the cap.
    Read those as the default / the cap instead of failing the call. Unknown keys still fail (extra="forbid")."""
    out = {}
    for key, value in payload.items():
        if value is None or (isinstance(value, str) and value.strip().lower() in NULLISH):
            continue
        field = model.model_fields.get(key)
        for le in [getattr(m, "le", None) for m in (field.metadata if field else [])]:
            try:
                if le is not None and float(value) > le:
                    value = le
            except (TypeError, ValueError):
                pass
        out[key] = value
    return out


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
        args = tool.args.model_validate(_lenient(tool.args, payload))
    except ValidationError as e:
        return {"error": "invalid_arguments", "details": e.errors(include_url=False, include_input=False)[:3]}, cart_lines
    try:
        if tool.uses_cart:  # cart tools copy the cart before changing it, so a retry is safe
            result, cart_lines = with_retry(tool.fn, ctx, args, cart_lines)
        else:
            result = with_retry(tool.fn, ctx, args)
    except PyMongoError:
        return {"error": DB_UNAVAILABLE}, cart_lines
    except Exception:  # noqa: BLE001 - never leak stack traces to the model
        return {"error": "temporarily_unavailable"}, cart_lines
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        return {"error": "result_too_large", "hint": "narrow the query or lower limit"}, cart_lines
    return {"as_of": utc_now_iso(), **result} if tool.uses_cart else result, cart_lines
