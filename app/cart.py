"""Cart (build-only, never submitted). Lives in the LangGraph state of one chat thread.

A cart line stores only {item_id, name, size, qty}; prices are re-read from the DB every time the cart
is shown, so totals are always live. Totals are menu subtotals: tax, delivery and discounts are added
by the app at checkout.
"""
import re
from typing import List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.data import repositories as repo
from app.retrieval import GENERIC_WORDS, index

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


class SetQuantityArgs(_Args):
    line_no: int = Field(..., ge=1, le=MAX_LINES)
    qty: int = Field(..., ge=1, le=MAX_QTY)


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


# ---------- did the CUSTOMER choose this item? ----------
# The model fills in item names itself ("add pasta" -> "Fettuccine Alfredo Pasta"). From chat, an item goes in
# the cart only if the customer's own words single it out, or point at something just shown ("the first one",
# "yeh wala"). Otherwise the cart asks which one. Card buttons are explicit choices and skip this.

# Words that say what KIND of thing, or how many / how to add, but never WHICH item.
KIND_WORDS = {"pasta", "pastas", "sandwich", "sandwiches", "platter", "platters", "drink", "drinks", "dessert",
              "desserts", "starter", "starters", "side", "sides", "fries", "wings", "dip", "dips", "crust"}
ADD_WORDS = {"add", "cart", "karo", "kar", "kardo", "daal", "dal", "dalo", "daalo", "to", "my", "in", "into", "ek",
             "aik", "two", "three", "four", "five", "plus", "also", "ko", "ka", "ki", "ke", "sa", "si", "please"}
NOT_A_CHOICE = GENERIC_WORDS | KIND_WORDS | ADD_WORDS
SIZE_WORDS = {"small", "regular", "medium", "large", "party", "family", "personal", "jumbo", "size"}
# A word that names a whole kind of item -> its type (app/ingestion/item_types.yaml).
CATEGORY_WORDS = {
    "burger": "burger", "burgers": "burger", "pizza": "pizza", "pizzas": "pizza", "pasta": "pasta",
    "pastas": "pasta", "sandwich": "sandwich or platter", "sandwiches": "sandwich or platter",
    "platter": "sandwich or platter", "platters": "sandwich or platter", "starter": "starter",
    "starters": "starter", "appetizer": "starter", "appetizers": "starter", "side": "side", "sides": "side",
    "fried chicken": "fried chicken", "dessert": "dessert", "desserts": "dessert", "sweet": "dessert",
    "sweets": "dessert", "drink": "drink or add-on", "drinks": "drink or add-on",
    "cold drink": "drink or add-on", "cold drinks": "drink or add-on", "beverages": "drink or add-on",
}
ORDINALS = {"first": 0, "1st": 0, "pehla": 0, "pehli": 0, "pehle": 0, "pahla": 0, "pahli": 0,
            "second": 1, "2nd": 1, "dusra": 1, "dusri": 1, "doosra": 1, "doosri": 1, "dusre": 1,
            "third": 2, "3rd": 2, "teesra": 2, "teesri": 2, "tisra": 2, "tisri": 2,
            "fourth": 3, "4th": 3, "chotha": 3, "chothi": 3, "chautha": 3,
            "fifth": 4, "5th": 4, "panchwa": 4, "panchwi": 4}
LAST_WORDS = {"last", "aakhri", "akhri", "akhiri"}
POINTING = {"it", "this", "that", "same", "yeh", "ye", "woh", "wo", "isko", "usko", "ise", "use", "isay", "usay",
            "issay", "yehi", "wohi"}
NUMBERED = re.compile(r"\b(?:number|no|option|#)\s*\.?\s*([1-9])\b", re.I)


def _key(name: str) -> str:
    return " ".join(_norm(name))


def pointed_item(said: str, offered: Tuple[dict, ...]) -> Optional[dict]:
    """The offered item the customer points at: "first one", "dusri wali", "option 2", "last one", or "add it"
    when exactly one thing was offered."""
    if not offered:
        return None
    words = _norm(said)
    for w in words:
        if w in ORDINALS and ORDINALS[w] < len(offered):
            return offered[ORDINALS[w]]
        if w in LAST_WORDS:
            return offered[-1]
    m = NUMBERED.search(said or "")
    if m and int(m.group(1)) <= len(offered):
        return offered[int(m.group(1)) - 1]
    if len(offered) == 1 and words:
        # "add it" / "yeh wala", or just a size after "which size?" ("large", "regular wala")
        filler = SIZE_WORDS | ADD_WORDS | POINTING | {"wala", "wali", "wale", "one", "haan", "ha", "yes", "ok", "okay"}
        if set(words) & POINTING or all(w in filler or w.isdigit() for w in words):
            return offered[0]
    return None


SIZE_SYNONYMS = {"chota": "small", "chhota": "small", "choti": "small", "bara": "large", "bada": "large",
                 "barra": "large", "bari": "large", "normal": "regular", "medium": "regular"}


def size_in(said: str, item: dict) -> Optional[str]:
    """A size of this item the customer typed ("large wala", "bara wala")."""
    words = {SIZE_SYNONYMS.get(w, w) for w in _norm(said)}
    return next((s["size"] for s in item.get("sizes") or [] if s["size"].lower() in words), None)


def chosen_from_offered(said: str, offered: Tuple[dict, ...]) -> Optional[dict]:
    """The one shown item the customer's reply names ("Crown Crust", "fettuccine wala"), if exactly one."""
    hits = [o for o in offered if named_by_customer(o, said)]
    return hits[0] if len(hits) == 1 else None


def named_by_customer(item: dict, said: str) -> bool:
    """The customer typed a word only this item's name has ("fettuccine", "reggy"), allowing for typos."""
    words = set(_norm(said)) - NOT_A_CHOICE
    if not words:
        return False
    words |= set(index().corrections(" ".join(words)).values())
    distinctive = {w for w in _norm(item["name"]) if w not in NOT_A_CHOICE and len(w) > 2}
    return bool(distinctive & words)


def _options(said: str, item: Optional[dict], branch_id: str, offered: Tuple[dict, ...]) -> List[dict]:
    """What the customer could mean. "add it" with several things shown: those things. Otherwise items that fit
    the customer's words, of the kind they named ("pasta") or of the model's pick."""
    words = _norm(said)
    if set(words) & POINTING and offered:
        found = repo.get_items([o["item_id"] for o in offered], branch_id)
    else:
        query = " ".join(w for w in words if w not in ADD_WORDS and w not in POINTING and not w.isdigit())
        query = query or (item or {}).get("type") or (item or {}).get("name") or ""
        found = repo.get_items([h["item_id"] for h in index().search(query, "menu", k=12, branch_id=branch_id)],
                               branch_id) if query else []
    kind = next((CATEGORY_WORDS[w] for w in words if w in CATEGORY_WORDS), None) or (item or {}).get("type")
    return _as_options([i for i in found + ([item] if item else []) if not kind or i.get("type") == kind])


def _as_options(items: List[dict]) -> List[dict]:
    """Available items as "which one?" options (shown as cards with Add buttons)."""
    return [{"item_id": i["item_id"], "name": i["name"], "type": i.get("type"), "category": i.get("category"),
             "price": i.get("price"), "price_from": i.get("price_from"), "sizes": i.get("sizes"),
             "available": True} for i in repo.merge_duplicates([i for i in items if i["available"]])[:6]]


def _which_one(said: str, options: List[dict]) -> dict:
    return {"error": "which_one", "customer_said": said[:120], "options": options,
            "note": "The customer did not say which one. Do not pick: list these options with type and price "
                    "and ask which one (and size, if it has sizes)."}


def resolve_for_chat(ctx, arg: CartItemArg) -> Tuple[Optional[dict], Optional[dict]]:
    """Which item the CUSTOMER chose (the model's name is only a hint): pointed at, named, or the only one of
    its kind. Otherwise a "which_one" problem with the options. Card buttons don't come through here."""
    said = ctx.user_message
    offered = tuple(getattr(ctx, "offered", ()) or ())
    pointed = pointed_item(said, offered)
    if pointed:
        live = repo.get_items([pointed["item_id"]], ctx.branch_id)
        if not live or not live[0]["available"]:
            return None, {"error": "not_available_at_branch", "requested": pointed["name"]}
        return live[0], None
    pointing_only = {"wala", "wali", "wale", "one", "number", "option", "haan", "yes", "ok", "okay"}
    real = [w for w in _norm(said) if w not in POINTING | set(ORDINALS) | LAST_WORDS | ADD_WORDS | SIZE_WORDS
            | pointing_only and not w.isdigit()]
    if not real and len(offered) > 1:  # "add it" / "large" with several things shown: which of those?
        return None, _which_one(said, _as_options(repo.get_items([o["item_id"] for o in offered], ctx.branch_id)))
    if not real:  # "pehli wali" / "add it" with nothing shown to point at
        return None, {"error": "unclear_which_item", "customer_said": said[:80],
                      "shown_last_turn": [o["name"] for o in offered],
                      "note": "Ask which item the customer means; don't guess."}
    item, problem = _resolve(arg, ctx.branch_id)
    if item and named_by_customer(item, said):
        return item, None
    options = _options(said, item, ctx.branch_id, offered)
    if len(options) == 1 and (item is None or options[0]["item_id"] == item["item_id"]):
        # the only item of that kind here: nothing to choose between
        return item or (repo.get_items([options[0]["item_id"]], ctx.branch_id) or [None])[0], None
    if not options:
        return None, problem or {"error": "not_found", "requested": said[:80]}
    return None, _which_one(said, options)


# ---------- tool implementations: (ctx, args, cart) -> (result, new_cart) ----------

def add_to_cart(ctx, a: AddToCartArgs, cart: List[dict]):
    cart = [dict(l) for l in cart]
    added, problems = [], []
    asked_about = []  # items we need a size for: shown as cards so "large" next turn means this item
    for arg in a.items:
        chat = getattr(ctx, "user_message", None) is not None
        item, problem = resolve_for_chat(ctx, arg) if chat else _resolve(arg, ctx.branch_id)
        if problem:
            problems.append(problem)
            continue
        size = None
        if item.get("sizes"):
            valid = [s["size"] for s in item["sizes"]]
            # From chat only the customer's own words pick a size (the model tends to assume "Regular").
            wanted = size_in(ctx.user_message, item) if chat else arg.size
            size = next((v for v in valid if wanted and v.lower() == wanted.strip().lower()), None)
            if not size:
                problems.append({"error": "size_required" if not wanted else "unknown_size", "item": item["name"],
                                 "item_id": item["item_id"], "valid_sizes": item["sizes"]})
                asked_about.append(item)
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
        options = [o for p in problems for o in p.get("options", [])] + asked_about
        if options:  # shown as cards with Add buttons
            result["items"] = options
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


def set_quantity(ctx, a: SetQuantityArgs, cart: List[dict]):
    """The app's +/- buttons. Removing a line is remove_from_cart; qty here is at least 1."""
    cart = [dict(l) for l in cart]
    if a.line_no > len(cart):
        return {"error": "not_in_cart", "cart": price_cart(cart, ctx.branch_id)}, cart
    cart[a.line_no - 1]["qty"] = a.qty
    return {"updated": {"line_no": a.line_no, "qty": a.qty}, "cart": price_cart(cart, ctx.branch_id)}, cart


def view_cart(ctx, a: EmptyArgs, cart: List[dict]):
    return {"cart": price_cart(cart, ctx.branch_id)}, cart


def clear_cart(ctx, a: EmptyArgs, cart: List[dict]):
    return {"cleared": True, "cart": price_cart([], ctx.branch_id)}, []
