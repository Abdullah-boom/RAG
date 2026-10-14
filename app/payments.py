"""Payment method choice for a cart. The chat never takes payment: it records which method the customer
wants (validated against the branch's live switches) and the app's checkout does the paying.

Card data never enters the chat (PCI DSS: a bot that sees card numbers is in scope). Online methods are
paid in the app's own secure checkout (XPay / HBL hosted pages); find_card_number() catches a customer who
types a card number anyway, before the model or the logs see it.

Tax depends on how the customer pays (branches.codTaxRate for cash, onlineTaxRate for online), so each
method carries its own estimate. Estimates only: delivery fee, discounts and vouchers are added at checkout.
"""
import re
from typing import List, Optional

# label (= orders.paymentMethod) -> how customers know it, and when it applies.
METHODS = {
    "COD": {"name": "Cash on delivery", "kind": "cash", "when": "delivery"},
    "COP": {"name": "Cash on pickup", "kind": "cash", "when": "pickup"},
    "CARD_MACHINE": {"name": "Card on delivery (card machine)", "kind": "card_on_delivery", "when": "delivery"},
    "JAZZ_CASH_DQR": {"name": "JazzCash QR on delivery", "kind": "wallet_on_delivery", "when": "delivery"},
    "EASYPAISA_DQR": {"name": "Easypaisa QR on delivery", "kind": "wallet_on_delivery", "when": "delivery"},
    "XPAY_CARD": {"name": "Debit / credit card (online)", "kind": "online", "when": "any"},
    "XPAY_EASYPAISA": {"name": "Easypaisa (online)", "kind": "online", "when": "any"},
    "XPAY_JAZZCASH": {"name": "JazzCash (online)", "kind": "online", "when": "any"},
    "PAYMENT_GATEWAY_HBL": {"name": "Card via HBL (online)", "kind": "online", "when": "any"},
}
CASH_TYPES = {"COD", "COP"}  # paymentType values taxed at the cash rate; ONLINE uses the online rate
ESTIMATE_NOTE = "Estimate: delivery fee, discounts and vouchers are added in the app at checkout."
SECURE_NOTE = "Online payments are made in the app's secure checkout. Never share card numbers or OTPs in chat."


def tax_estimate(subtotal: int, rate: Optional[float], exclusive: bool) -> dict:
    """Exclusive: tax is added on top of menu prices. Inclusive: menu prices already contain it."""
    if rate is None:
        return {"tax_rate": None, "tax": None, "total": None}
    if exclusive:
        tax = round(subtotal * rate / 100)
        return {"tax_rate": rate, "tax": tax, "total": subtotal + tax}
    return {"tax_rate": rate, "tax": round(subtotal * rate / (100 + rate)), "total": subtotal}


def options(config: Optional[dict], subtotal: int = 0) -> List[dict]:
    """Methods this branch accepts right now, cheapest total first (online often has a lower tax rate)."""
    if not config:
        return []
    out = []
    for m in config["methods"]:
        meta = METHODS.get(m["label"])
        if not m["available"] or not meta:  # switched off, or a new method we can't describe yet
            continue
        cash = m["payment_type"] in CASH_TYPES
        rate = config["cash_tax_rate"] if cash else config["online_tax_rate"]
        out.append({"code": m["label"], **meta, "pay_now": meta["kind"] == "online",
                    **tax_estimate(subtotal, rate, config["tax_exclusive"])})
    return sorted(out, key=lambda o: (o["total"] if o["total"] is not None else float("inf"), o["name"]))


# Words that name a method ("cash", "card", "jazzcash", "delivery"...); fillers like "on" and "karunga" are ignored.
_FILLER = {"on", "via", "debit", "credit"}
METHOD_WORDS = {w for m in METHODS.values() for w in re.findall(r"[a-z]+", m["name"].lower())} - _FILLER


def _words(text: str) -> set:
    return set(re.findall(r"[a-z]+", text.lower()))


def find(config: Optional[dict], code_or_name: str) -> Optional[dict]:
    """An available method by code ("COD") or by how a customer says it ("cash on delivery", "online card").
    Every method word the customer used must be in the method's name, and only one method may fit: "card"
    alone is ambiguous (machine at the door or online?), and "jazzcash online" never falls back to another
    online method when JazzCash is switched off."""
    wanted = (code_or_name or "").strip().lower()
    opts = options(config)
    for o in opts:
        if wanted in (o["code"].lower(), o["name"].lower()):
            return o
    key = _words(wanted) & METHOD_WORDS
    if not key:
        return None
    hits = [o for o in opts if key <= _words(o["name"])]
    return hits[0] if len(hits) == 1 else None


def candidates(config: Optional[dict], said: str) -> List[dict]:
    """Available methods that fit the customer's words ("card" -> card machine at the door, online card)."""
    key = _words(said or "") & METHOD_WORDS
    return [o for o in options(config) if key and key <= _words(o["name"])]


def checkout(config: Optional[dict], subtotal: int, code: Optional[str]) -> dict:
    """What the cart shows: the chosen method (if still available) with its estimate."""
    chosen = next((o for o in options(config, subtotal) if o["code"] == code), None) if code else None
    return {"payment_method": chosen, "note": ESTIMATE_NOTE, "secure_note": SECURE_NOTE}


# ---------- card numbers typed into the chat ----------

CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def find_card_number(text: str) -> bool:
    """13-19 digits (spaces/dashes allowed) passing the Luhn check: almost certainly a payment card."""
    return any(_luhn(re.sub(r"\D", "", m.group())) for m in CARD_CANDIDATE.finditer(text or ""))


def redact_card_numbers(text: str) -> str:
    return CARD_CANDIDATE.sub(lambda m: "[card number removed]" if _luhn(re.sub(r"\D", "", m.group())) else m.group(),
                              text or "")
