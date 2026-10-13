"""Cart (build-only, never submitted). Lives in the LangGraph state of one chat thread.

A cart line stores only {item_id, name, size, qty}; prices are re-read from the DB every time the cart
is shown, so totals are always live. Totals are menu subtotals: tax, delivery and discounts are added
by the app at checkout.
"""
import re
from typing import List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.data import repositories as repo
from app.retrieval import index

MAX_LINES = 20
MAX_QTY = 50
TOTAL_NOTE = ("Menu subtotal only. Tax, delivery fee and discounts are added in the app at checkout. "
              "This cart is not an order; the customer places the order in the app.")
TOKEN = re.compile(r"\w+", re.UNICODE)


def _norm(text: str) -> List[str]:
    return TOKEN.findall((text or "").lower())


# ---------- argument models ----------

class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CartItemArg(_Args):
    name: Optional[str] = Field(None, max_length=80, description="Item name as on the menu, e.g. 'Chicken Tikka'.")
    item_id: Optional[str] = Field(None, max_length=40, description="item_id, if known from a previous result.")
    size: Optional[str] = Field(None, max_length=40, description="Size name, e.g. 'Regular'. Required if the item has sizes.")
    qty: int = Field(..., ge=1, le=MAX_QTY, description="How many the customer wants (required).")

    @model_validator(mode="after")
    def _name_or_id(self):
        if not (self.name or self.item_id):
            raise ValueError("give name or item_id")
        return self


class AddToCartArgs(_Args):
    items: List[CartItemArg] = Field(..., min_length=1, max_length=10)


class RemoveFromCartArgs(_Args):
    line_no: Optional[int] = Field(None, ge=1, le=MAX_LINES, description="Line number from view_cart.")
    name: Optional[str] = Field(None, max_length=80, description="Item name in the cart.")
    size: Optional[str] = Field(None, max_length=40)
    qty: Optional[int] = Field(None, ge=1, le=MAX_QTY, description="How many to remove; omit to remove the whole line.")


class EmptyArgs(_Args):
    pass


# ---------- pricing ----------

def _unit_price(item: dict, size: Optional[str]) -> Optional[int]:
    if item.get("sizes"):
        match = [s for s in item["sizes"] if size and s["size"].lower() == size.lower()]
        return match[0]["price"] if match else None
    return item.get("price")


def price_cart(cart: List[dict], branch_id: str) -> dict:
    """The cart with live prices. Lines no longer available are shown but not counted."""
    live = {i["item_id"]: i for i in repo.get_items(list({l["item_id"] for l in cart}), branch_id)} if cart else {}
    lines, subtotal = [], 0
    for n, line in enumerate(cart, 1):
        item = live.get(line["item_id"])
        unit = _unit_price(item, line.get("size")) if item and item["available"] else None
        row = {"line_no": n, "name": line["name"], "size": line.get("size"), "qty": line["qty"],
               "unit_price": unit, "line_total": unit * line["qty"] if unit is not None else None,
               "available": unit is not None}
        if unit is not None:
            subtotal += row["line_total"]
        lines.append(row)
    return {"lines": lines, "item_count": sum(l["qty"] for l in lines if l["available"]),
            "subtotal": subtotal, "currency": repo.CURRENCY, "note": TOTAL_NOTE}


# ---------- resolving what the customer means ----------

def _resolve(arg: CartItemArg, branch_id: str) -> Tuple[Optional[dict], Optional[dict]]:
    """Return (live item, None) or (None, problem) for one requested item."""
    if arg.item_id:
        items = repo.get_items([arg.item_id], branch_id)
        if not items or not items[0]["available"]:
            return None, {"error": "not_available_at_branch", "requested": arg.item_id}
        return items[0], None
    hits = index().search(arg.name, "menu", k=8, branch_id=branch_id)
    items = [i for i in repo.get_items([h["item_id"] for h in hits], branch_id) if i["available"]]
    wanted = set(_norm(arg.name))
    # Items whose whole name appears in the request ("chicken tikka pizza" -> "Chicken Tikka"); longest wins.
    named = [i for i in items if set(_norm(i["name"])) <= wanted]
    if named:
        best_len = max(len(_norm(i["name"])) for i in named)
        named = [i for i in named if len(_norm(i["name"])) == best_len]
        variants = {(str(i.get("sizes")), i.get("price")) for i in named}
        if len(variants) == 1:  # duplicates with identical prices are the same thing to the customer
            return named[0], None
        return None, {"error": "ambiguous", "requested": arg.name, "options": [
            {"item_id": i["item_id"], "name": i["name"], "category": i.get("category"),
             "price": i.get("price"), "sizes": i.get("sizes")} for i in named[:4]]}
    if not items:
        return None, {"error": "not_found", "requested": arg.name}
    return None, {"error": "no_exact_match", "requested": arg.name,
                  "did_you_mean": [{"item_id": i["item_id"], "name": i["name"], "category": i.get("category")}
                                   for i in items[:3]]}


# ---------- tool implementations: (ctx, args, cart) -> (result, new_cart) ----------

def add_to_cart(ctx, a: AddToCartArgs, cart: List[dict]):
    cart = [dict(l) for l in cart]
    added, problems = [], []
    for arg in a.items:
        item, problem = _resolve(arg, ctx.branch_id)
        if problem:
            problems.append(problem)
            continue
        size = None
        if item.get("sizes"):
            valid = [s["size"] for s in item["sizes"]]
            size = next((v for v in valid if arg.size and v.lower() == arg.size.strip().lower()), None)
            if not size:
                problems.append({"error": "size_required" if not arg.size else "unknown_size",
                                 "item": item["name"], "valid_sizes": item["sizes"]})
                continue
        existing = next((l for l in cart if l["item_id"] == item["item_id"] and l.get("size") == size), None)
        if existing:
            existing["qty"] = min(existing["qty"] + arg.qty, MAX_QTY)
        elif len(cart) >= MAX_LINES:
            problems.append({"error": "cart_full", "max_lines": MAX_LINES})
            continue
        else:
            cart.append({"item_id": item["item_id"], "name": item["name"], "size": size, "qty": arg.qty})
        added.append({"name": item["name"], "size": size, "qty": arg.qty})
    result = {"added": added, "cart": price_cart(cart, ctx.branch_id)}
    if problems:
        result["problems"] = problems
    return result, cart


def _find_line(cart: List[dict], a: RemoveFromCartArgs) -> Optional[int]:
    if a.line_no:
        return a.line_no - 1 if a.line_no <= len(cart) else None
    if a.name:
        wanted = set(_norm(a.name))
        for i, l in enumerate(cart):
            if set(_norm(l["name"])) <= wanted or wanted <= set(_norm(l["name"])):
                if not a.size or (l.get("size") or "").lower() == a.size.lower():
                    return i
    return None


def remove_from_cart(ctx, a: RemoveFromCartArgs, cart: List[dict]):
    cart = [dict(l) for l in cart]
    i = _find_line(cart, a)
    if i is None:
        return {"error": "not_in_cart", "cart": price_cart(cart, ctx.branch_id)}, cart
    line = cart[i]
    if a.qty and a.qty < line["qty"]:
        line["qty"] -= a.qty
        removed = {"name": line["name"], "size": line.get("size"), "qty": a.qty}
    else:
        removed = cart.pop(i)
        removed = {"name": removed["name"], "size": removed.get("size"), "qty": removed["qty"]}
    return {"removed": removed, "cart": price_cart(cart, ctx.branch_id)}, cart


def view_cart(ctx, a: EmptyArgs, cart: List[dict]):
    return {"cart": price_cart(cart, ctx.branch_id)}, cart


def clear_cart(ctx, a: EmptyArgs, cart: List[dict]):
    return {"cleared": True, "cart": price_cart([], ctx.branch_id)}, []
