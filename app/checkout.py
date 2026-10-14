"""Order confirmation and hand-off. The chatbot never creates orders or takes payment: it checks that the
cart is ready, the customer explicitly confirms, and it hands the cart state to the order backend
(ORDER_BACKEND_URL), which places the order, takes any payment in its own flow and answers with a status.

Review -> confirm is two steps on purpose: review returns a short-lived confirm token signed over exactly
that cart and payment choice, so a cart changed after the customer saw the summary can't be confirmed, and
a double tap sends one order (the token is also the backend's idempotency key).
"""
import hashlib
import hmac
import json
import logging
import os
import time
from typing import List, Optional

import httpx

from app import config, payments

log = logging.getLogger("chatbot.checkout")
TOKEN_TTL_S = 15 * 60
BACKEND_TIMEOUT_S = 15
# Signs confirm tokens. JWT_SECRET when set (shared by workers); else per process (dev, one worker).
_SECRET = (config.CHECKOUT_SECRET or config.JWT_SECRET or os.urandom(32).hex()).encode()

# Outcome codes the apps switch on.
PLACED = "placed"                 # the order backend accepted it: order_no is set, the cart is emptied
NOT_CONNECTED = "not_connected"   # no ORDER_BACKEND_URL yet: the customer completes the order in the app
REJECTED = "rejected"             # the backend said no (e.g. item sold out): message says why, cart kept
FAILED = "failed"                 # backend unreachable / error: safe to try again with the same token


# ---------- review ----------

def review(*, user_id: Optional[str], branch_status: Optional[dict], cart: Optional[dict],
           payment_config: Optional[dict], payment_code: Optional[str]) -> dict:
    """Is this cart ready to order? `cart` is a priced cart (app/cart.py price_cart) or None."""
    problems: List[dict] = []
    if not cart or not cart["lines"]:
        problems.append({"code": "empty_cart"})
    else:
        gone = [l["name"] for l in cart["lines"] if not l["available"]]
        if gone:
            problems.append({"code": "unavailable_items", "items": gone})
    if not branch_status or not branch_status.get("open_now"):
        problems.append({"code": "branch_closed"})
    subtotal = cart["subtotal"] if cart else 0
    minimum = (branch_status or {}).get("min_order_amount")
    if cart and cart["lines"] and minimum and subtotal < minimum:
        problems.append({"code": "below_minimum_order", "minimum": minimum, "subtotal": subtotal})
    method = payments.checkout(payment_config, subtotal, payment_code)["payment_method"]
    if not payment_code:
        problems.append({"code": "no_payment_method"})
    elif not method:
        problems.append({"code": "payment_method_unavailable", "method": payment_code})
    if not user_id:
        problems.append({"code": "login_required"})  # the order belongs to a customer account
    summary = {"lines": (cart or {}).get("lines", []), "subtotal": subtotal, "payment_method": method,
               "note": payments.ESTIMATE_NOTE}
    return {"ready": not problems, "problems": problems, "summary": summary}


# ---------- confirm token ----------

def fingerprint(owner: str, branch_id: str, lines: List[dict], payment_code: Optional[str]) -> str:
    state = json.dumps({"o": owner, "b": branch_id, "p": payment_code,
                        "l": [[l["item_id"], l.get("size"), l["qty"]] for l in lines]}, sort_keys=True)
    return hashlib.sha256(state.encode()).hexdigest()


def make_token(fp: str, now: Optional[float] = None) -> str:
    exp = int((time.time() if now is None else now) + TOKEN_TTL_S)
    sig = hmac.new(_SECRET, f"{exp}.{fp}".encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def check_token(token: str, fp: str, now: Optional[float] = None) -> Optional[str]:
    """None if valid, else "expired" or "cart_changed" (signature doesn't match the cart as it is now)."""
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return "cart_changed"
    good = hmac.new(_SECRET, f"{exp}.{fp}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return "cart_changed"
    return "expired" if (time.time() if now is None else now) > exp else None


# ---------- hand-off ----------

def order_request(*, user_id: str, branch_id: str, lines: List[dict], payment_method: dict, token: str) -> dict:
    """What the order backend receives: state only (ids, sizes, quantities, payment code), no payment data.
    Prices are the backend's to set; the estimate is there so it can flag a mismatch."""
    return {"source": "chatbot", "customer_id": user_id, "branch_id": branch_id,
            "items": [{"item_id": l["item_id"], "name": l["name"], "size": l.get("size"), "qty": l["qty"]}
                      for l in lines],
            "payment_method": payment_method["code"],
            "estimate": {"tax_rate": payment_method["tax_rate"], "tax": payment_method["tax"],
                         "total": payment_method["total"]},
            "idempotency_key": token}


def submit(payload: dict, customer_auth: Optional[str]) -> dict:
    """Send the order to ORDER_BACKEND_URL. Expected reply (adjust here when the contract is fixed):
    2xx JSON {status?, order_no | orderNo, message?}; 4xx = rejected with a message; else failed."""
    if not config.ORDER_BACKEND_URL:
        return {"status": NOT_CONNECTED, "order_no": None, "message": None}
    headers = {"Idempotency-Key": payload["idempotency_key"]}
    if config.ORDER_BACKEND_KEY:
        headers["Authorization"] = f"Bearer {config.ORDER_BACKEND_KEY}"
    if customer_auth:
        headers["X-Customer-Authorization"] = customer_auth  # lets the backend verify the customer itself
    try:
        r = httpx.post(config.ORDER_BACKEND_URL, json=payload, headers=headers, timeout=BACKEND_TIMEOUT_S)
    except httpx.HTTPError as e:
        log.warning("order backend unreachable: %s", type(e).__name__)
        return {"status": FAILED, "order_no": None, "message": None}
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    message = body.get("message") if isinstance(body.get("message"), str) else None
    if r.is_success:
        return {"status": PLACED, "order_no": body.get("order_no") or body.get("orderNo"),
                "backend_status": body.get("status"), "message": message}
    if 400 <= r.status_code < 500:
        return {"status": REJECTED, "order_no": None, "message": message}
    log.warning("order backend error %s", r.status_code)
    return {"status": FAILED, "order_no": None, "message": None}
