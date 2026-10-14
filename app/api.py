"""HTTP API for the chat widget (website or the MERN app).  Run: make api   (or uvicorn app.api:app)

Identity: the customer id comes only from a verified `Authorization: Bearer <jwt>` (app/auth.py); no
header = guest, and order tools then answer "please log in". It is never read from the request body.

A conversation is keyed by (branch_id, customer or "guest", session_id). The server mints session_id; the
widget only stores it. Switching branch or login therefore starts a fresh conversation, and a leaked
session id is useless to anyone else.

Carts are separate from conversations: one per (customer, branch), see app/cart_store.py. A logged-in
customer's carts follow their account; a guest's follow the `X-Guest-Id` header (server-minted, returned
as `guest_id`; the app stores it and sends it back). Switching branch loads that branch's cart and leaves
every other branch's cart untouched; "new chat" keeps the cart.

Payment: the customer picks a method per cart (GET /api/payment-methods, POST /api/cart/payment, or by
telling the bot). It is validated against the branch's live switches and shown with an estimated tax and
total (tax differs by method). Nothing is charged here and card details never enter the chat: the app's
checkout takes the payment.

Ordering: the bot never places orders. POST /api/checkout/review checks the cart is ready and returns a
summary with a confirm token; POST /api/checkout/confirm (an explicit tap) hands the cart state to the order
backend (ORDER_BACKEND_URL), which places the order and answers with a status (app/checkout.py).

Branch choice: POST /api/branches/locate turns the device's coordinates into a default branch using the
delivery areas in product.polygons; POST /api/branches/select checks on the server that a branch is open
before the app switches to it, and returns that branch's cart.

POST /api/chat with "stream": true answers as Server-Sent Events: `status` lines while the agent works,
then one `answer` (or `error`). The answer is sent whole, after the price guard has passed.
"""
import asyncio
import json
import logging
import re
import threading
import time
import uuid
import warnings
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Deque, Dict, List, Optional

warnings.filterwarnings("ignore")

import jwt as pyjwt  # noqa: E402
from fastapi import FastAPI, HTTPException, Query, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from pymongo.errors import PyMongoError  # noqa: E402

from app import config  # noqa: E402
from app.agent.graph import chat, chat_stream, graph  # noqa: E402
from app.agent.guards import MAX_MESSAGE_CHARS  # noqa: E402
from app.auth import user_from_request  # noqa: E402
from app import cart as cart_mod  # noqa: E402
from app import locator  # noqa: E402
from app import checkout, payments  # noqa: E402
from app.cart import price_cart  # noqa: E402
from app.cart_store import CartBusy, CartUnavailable, owner_key, store as cart_store  # noqa: E402
from app.data import repositories as repo  # noqa: E402
from app.data.db import with_retry  # noqa: E402
from app.tools import DB_UNAVAILABLE, RequestContext  # noqa: E402
from app.retrieval import index  # noqa: E402

log = logging.getLogger("chatbot.api")
SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
GUEST_HEADER = "X-Guest-Id"
BRANCH_CACHE_S = 300
WIDGET_DIR = config.ROOT / "widget" / "dist"


def warm_up() -> None:
    index().search("pizza", "menu", k=1)  # load the embedding model before the first customer
    graph()  # connect the checkpointer (Redis) at startup, not on the first message


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # In a thread: the model load can take a minute on a busy host, and a blocked event loop can't answer
    # uvicorn's worker health pings, so with --workers > 1 the supervisor would kill and restart it forever.
    await asyncio.to_thread(warm_up)
    yield


# Interactive docs only when API_DEBUG=true; production exposes just the routes below.
app = FastAPI(title="Cheezious Assistant API", version="1.0", lifespan=lifespan, redoc_url=None,
              docs_url="/api/docs" if config.API_DEBUG else None,
              openapi_url="/api/openapi.json" if config.API_DEBUG else None)
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["GET", "POST"],
                   allow_headers=["Content-Type", "Authorization", GUEST_HEADER], allow_credentials=False, max_age=600)


# ---------- helpers ----------

_branches: Dict[str, object] = {"at": 0.0, "rows": []}
_branch_lock = threading.Lock()


def db_retry(fn, *args):
    """One quick retry for a dropped Atlas connection, then 503 so the widget can say "try again"."""
    try:
        return with_retry(fn, *args)
    except PyMongoError:
        raise HTTPException(503, DB_UNAVAILABLE)


def branches() -> List[dict]:
    with _branch_lock:
        if time.time() - _branches["at"] > BRANCH_CACHE_S or not _branches["rows"]:
            try:
                rows = db_retry(repo.list_branches)
            except HTTPException:
                if _branches["rows"]:  # DB blip: keep serving the last good list
                    return _branches["rows"]
                raise
            rows = [b for b in rows if not repo.TEST_NAME.search(b["name"])]  # dev test branches
            _branches["rows"] = sorted(rows, key=lambda b: ((b["city"] or ""), b["name"]))
            _branches["at"] = time.time()
        return _branches["rows"]


def require_branch(branch_id: str) -> dict:
    b = next((b for b in branches() if b["branch_id"] == branch_id), None)
    if not b:
        raise HTTPException(404, "unknown_branch")
    return b


def public_branch(b: dict) -> dict:
    """What the apps see of a branch. open_now is computed per request; the row itself is cached."""
    return {"branch_id": b["branch_id"], "name": b["name"], "city": b["city"], "open_now": repo.open_now(b),
            "accepting_orders": b["accepting_orders"]}


# ---------- cart owner ----------

def valid_guest(guest_id: Optional[str]) -> Optional[str]:
    return guest_id if guest_id and SESSION_ID.match(guest_id) else None


def cart_owner(request: Request, mint: bool) -> tuple:
    """(user_id, owner, guest_id). Logged in: the account owns the cart. Guest: the X-Guest-Id header does;
    mint=True creates one for a first-time guest (writes), mint=False leaves owner None (reads: empty cart)."""
    user_id = user_from_request(request)
    guest_id = valid_guest(request.headers.get(GUEST_HEADER))
    if not user_id and not guest_id and mint:
        guest_id = uuid.uuid4().hex
    owner = owner_key(user_id, guest_id) if (user_id or guest_id) else None
    return user_id, owner, guest_id


def store_call(fn, *args):
    try:
        return fn(*args)
    except CartUnavailable:
        raise HTTPException(503, "cart_unavailable")


PAYMENT_CACHE_S = 60
_payment_configs: Dict[str, tuple] = {}


def payment_config(branch_id: str) -> Optional[dict]:
    """The branch's payment switches and tax rates, cached briefly: every cart response shows an estimate."""
    hit = _payment_configs.get(branch_id)
    if hit and time.time() - hit[0] < PAYMENT_CACHE_S:
        return hit[1]
    cfg = db_retry(repo.get_payment_config, branch_id)
    _payment_configs[branch_id] = (time.time(), cfg)
    return cfg


def with_checkout(cart: Optional[dict], branch_id: str, method: Optional[str]) -> Optional[dict]:
    """Add the chosen payment method and its tax/total estimate to a priced cart."""
    if not cart:
        return cart
    return {**cart, "checkout": payments.checkout(payment_config(branch_id), cart["subtotal"], method)}


def owner_payment(owner: Optional[str], branch_id: str) -> Optional[str]:
    return store_call(cart_store().get_payment, owner, branch_id) if owner else None


def priced(lines: list, branch_id: str, method: Optional[str] = None) -> Optional[dict]:
    return with_checkout(db_retry(price_cart, lines, branch_id), branch_id, method) if lines else None


def thread_id(branch_id: str, user_id: Optional[str], session_id: str) -> str:
    return f"{branch_id}:{user_id or 'guest'}:{session_id}"


def thread_config(branch_id: str, user_id: Optional[str], session_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id(branch_id, user_id, session_id)}}


def valid_session(session_id: Optional[str]) -> Optional[str]:
    return session_id if session_id and SESSION_ID.match(session_id) else None


class RateLimiter:
    """Sliding one-minute window per key (client IP, and customer when logged in). In-process: with several workers each counts separately."""

    def __init__(self, per_min: int) -> None:
        self.per_min = per_min
        self.hits: Dict[str, Deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_min:
                return False
            q.append(now)
            if len(self.hits) > 10_000:  # drop idle clients so memory stays bounded
                for k in [k for k, v in self.hits.items() if not v or now - v[-1] > 60]:
                    del self.hits[k]
            return True


limiter = RateLimiter(config.RATE_LIMIT_PER_MIN)


def client_ip(request: Request) -> str:
    if config.TRUST_PROXY and request.headers.get("x-forwarded-for"):
        return request.headers["x-forwarded-for"].split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------- models ----------

class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_CHARS)
    branch_id: str = Field(..., max_length=40)
    session_id: Optional[str] = Field(None, max_length=64)
    stream: bool = False


class BranchIn(BaseModel):
    branch_id: str = Field(..., max_length=40)


class LocateIn(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="Latitude in degrees (WGS84, as phones report it)")
    lng: float = Field(..., ge=-180, le=180, description="Longitude in degrees (WGS84)")


# ---------- routes ----------

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/branches")
def list_branches():
    """Active branches with open_now; only open ones can be selected (POST /api/branches/select)."""
    return {"branches": [public_branch(b) for b in branches()]}


@app.post("/api/branches/locate")
def locate_branch(body: LocateIn, request: Request):
    """Default branch for the device's location: the open branch whose delivery area contains the point.
    reason: "matched" (branch set) | "branch_closed" | "no_coverage" (branch null: ask the customer to pick).
    POST, not GET, so coordinates stay out of URLs and access logs; they are not stored or logged."""
    if not limiter.allow(f"ip:{client_ip(request)}"):
        raise HTTPException(429, "too_many_requests")
    rows = branches()
    found = locator.choose_branch(body.lat, body.lng, db_retry(repo.branches_covering, body.lat, body.lng), rows)
    by_id = {b["branch_id"]: b for b in rows}
    return {"reason": found["reason"],
            "branch": public_branch(by_id[found["branch_id"]]) if found["branch_id"] else None,
            "covering": [public_branch(by_id[i]) for i in found["covering"]]}


@app.post("/api/branches/select")
def select_branch(body: BranchIn, request: Request):
    """Switch to a branch. Checked here, not just in the app: it must be active and open right now (live
    read, not the cached list). Returns that branch's status and its own cart; other carts are untouched."""
    require_branch(body.branch_id)
    status = db_retry(repo.get_branch_status, body.branch_id)
    if not status or not status["open_now"]:
        raise HTTPException(409, "branch_closed")
    _, owner, guest_id = cart_owner(request, mint=False)
    lines = store_call(cart_store().get, owner, body.branch_id) if owner else []
    return {"branch": public_branch(require_branch(body.branch_id)), "status": status, "guest_id": guest_id,
            "cart": priced(lines, body.branch_id, owner_payment(owner, body.branch_id))}


@app.get("/api/branches/{branch_id}/status")
def branch_status(branch_id: str):
    require_branch(branch_id)
    return db_retry(repo.get_branch_status, branch_id)


def public_response(response: dict, session_id: str, guest_id: Optional[str]) -> dict:
    out = {k: response.get(k) for k in ("answer", "items", "suggestions", "cart", "handoff", "as_of")}
    if config.API_DEBUG:
        out["debug"] = response.get("debug")
    return {"session_id": session_id, "guest_id": guest_id, **out}


def save_turn_cart(owner: str, branch_id: str, response: dict, method: Optional[str]) -> dict:
    """Save the turn's cart (and any payment choice) and return the response with the checkout estimate."""
    if "cart_lines" in response:
        store_call(cart_store().set, owner, branch_id, response["cart_lines"])
    if response.get("payment_method_set"):
        method = response["payment_method_set"]
        store_call(cart_store().set_payment, owner, branch_id, method)
    return {**response, "cart": with_checkout(response.get("cart"), branch_id, method)}


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@app.post("/api/chat")
def post_chat(body: ChatIn, request: Request):
    user_id, owner, guest_id = cart_owner(request, mint=True)
    if not limiter.allow(f"ip:{client_ip(request)}") or (user_id and not limiter.allow(f"user:{user_id}")):
        raise HTTPException(429, "too_many_messages")
    require_branch(body.branch_id)
    session_id = valid_session(body.session_id) or uuid.uuid4().hex
    args = dict(thread_id=thread_id(body.branch_id, user_id, session_id), branch_id=body.branch_id,
                user_id=user_id)
    message = body.message.strip()
    carts = cart_store()

    if body.stream:
        def events():
            # The cart stays locked for the whole turn, so a cart button pressed meanwhile waits for it.
            try:
                with carts.lock(owner, body.branch_id):
                    lines = carts.get(owner, body.branch_id)
                    method = carts.get_payment(owner, body.branch_id)
                    for ev in chat_stream(message, cart=lines, payment_method=method, **args):
                        if "status" in ev:
                            yield sse("status", {"text": ev["status"]})
                        elif ev["response"].get("error") == DB_UNAVAILABLE:
                            yield sse("error", {"code": DB_UNAVAILABLE})
                        else:
                            reply = save_turn_cart(owner, body.branch_id, ev["response"], method)
                            yield sse("answer", public_response(reply, session_id, guest_id))
            except CartBusy:
                yield sse("error", {"code": "cart_busy"})
            except (CartUnavailable, HTTPException):
                yield sse("error", {"code": "cart_unavailable"})
            except Exception:  # noqa: BLE001 - headers are already sent: report in-band, never hang
                log.exception("chat stream failed")
                yield sse("error", {"code": "internal_error"})

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    try:
        with carts.lock(owner, body.branch_id):
            method = store_call(carts.get_payment, owner, body.branch_id)
            response = chat(message, cart=store_call(carts.get, owner, body.branch_id), payment_method=method, **args)
            if response.get("error") == DB_UNAVAILABLE:
                raise HTTPException(503, DB_UNAVAILABLE)  # the turn was discarded; the widget offers "Try again"
            response = save_turn_cart(owner, body.branch_id, response, method)
    except CartBusy:
        raise HTTPException(409, "cart_busy")
    except CartUnavailable:
        raise HTTPException(503, "cart_unavailable")
    return public_response(response, session_id, guest_id)


@app.get("/api/session")
def get_session(request: Request, branch_id: str = Query(..., max_length=40),
                session_id: str = Query(..., max_length=64)):
    """Restore a conversation after a page reload: question/answer text + this branch's live-priced cart.
    Only the customer who owns it can read it: the thread key includes their verified id."""
    user_id, owner, _ = cart_owner(request, mint=False)
    require_branch(branch_id)
    if not valid_session(session_id):
        raise HTTPException(404, "unknown_session")
    state = graph().get_state(thread_config(branch_id, user_id, session_id)).values
    if not state:
        raise HTTPException(404, "unknown_session")
    messages = [{"role": "user" if isinstance(m, HumanMessage) else "assistant", "text": m.content}
                for m in state.get("messages", []) if isinstance(m, (HumanMessage, AIMessage)) and m.content]
    lines = store_call(cart_store().get, owner, branch_id) if owner else []
    return {"session_id": session_id, "messages": messages,
            "cart": priced(lines, branch_id, owner_payment(owner, branch_id))}


# ---------- cart: no LLM ----------
# The app's "Add to cart" / size buttons, +/- and remove buttons and "Show my cart" call these directly: plain
# data operations, so they work instantly even when the LLM is rate limited, and cost no LLM tokens. They edit
# the same per-branch cart the chat's cart tools use. Each call names its branch and touches only that cart.

class CartAddIn(BaseModel):
    branch_id: str = Field(..., max_length=40)
    item_id: str = Field(..., max_length=40)
    size: Optional[str] = Field(None, max_length=40)
    qty: int = Field(1, ge=1, le=cart_mod.MAX_QTY)


class CartLineIn(BaseModel):
    branch_id: str = Field(..., max_length=40)
    line_no: int = Field(..., ge=1, le=cart_mod.MAX_LINES)


class CartQtyIn(CartLineIn):
    qty: int = Field(..., ge=1, le=cart_mod.MAX_QTY)


def _cart_action(request: Request, branch_id: str, fn, args):
    user_id, owner, guest_id = cart_owner(request, mint=True)
    if not limiter.allow(f"ip:{client_ip(request)}"):
        raise HTTPException(429, "too_many_requests")
    require_branch(branch_id)
    carts = cart_store()
    try:
        with carts.lock(owner, branch_id):
            lines = carts.get(owner, branch_id)
            result, new_lines = db_retry(fn, RequestContext(user_id, branch_id), args, lines)
            if new_lines != lines:
                carts.set(owner, branch_id, new_lines)
            method = carts.get_payment(owner, branch_id)
    except CartBusy:
        raise HTTPException(409, "cart_busy")
    except CartUnavailable:
        raise HTTPException(503, "cart_unavailable")
    cart = with_checkout(result.get("cart"), branch_id, method) if new_lines else None
    return {"guest_id": guest_id, **result, "cart": cart}


@app.get("/api/cart")
def get_cart(request: Request, branch_id: str = Query(..., max_length=40)):
    _, owner, guest_id = cart_owner(request, mint=False)
    require_branch(branch_id)
    lines = store_call(cart_store().get, owner, branch_id) if owner else []
    return {"guest_id": guest_id, "cart": priced(lines, branch_id, owner_payment(owner, branch_id))}


@app.post("/api/cart/add")
def cart_add(body: CartAddIn, request: Request):
    args = cart_mod.AddToCartArgs(items=[cart_mod.CartItemArg(item_id=body.item_id, size=body.size, qty=body.qty)])
    return _cart_action(request, body.branch_id, cart_mod.add_to_cart, args)


@app.post("/api/cart/quantity")
def cart_quantity(body: CartQtyIn, request: Request):
    args = cart_mod.SetQuantityArgs(line_no=body.line_no, qty=body.qty)
    return _cart_action(request, body.branch_id, cart_mod.set_quantity, args)


@app.post("/api/cart/remove")
def cart_remove(body: CartLineIn, request: Request):
    args = cart_mod.RemoveFromCartArgs(line_no=body.line_no)
    return _cart_action(request, body.branch_id, cart_mod.remove_from_cart, args)


@app.post("/api/cart/clear")
def clear_cart(body: BranchIn, request: Request):
    return _cart_action(request, body.branch_id, cart_mod.clear_cart, cart_mod.EmptyArgs())


# ---------- payment method (chosen here, paid in the app's checkout) ----------

class PaymentIn(BaseModel):
    branch_id: str = Field(..., max_length=40)
    method: str = Field(..., max_length=40, description="A code from GET /api/payment-methods, e.g. COD")


@app.get("/api/payment-methods")
def payment_methods(request: Request, branch_id: str = Query(..., max_length=40)):
    """Methods this branch accepts now, cheapest first, each with estimated tax and total for this cart."""
    _, owner, guest_id = cart_owner(request, mint=False)
    require_branch(branch_id)
    lines = store_call(cart_store().get, owner, branch_id) if owner else []
    subtotal = db_retry(price_cart, lines, branch_id)["subtotal"] if lines else 0
    return {"guest_id": guest_id, "methods": payments.options(payment_config(branch_id), subtotal),
            "chosen": owner_payment(owner, branch_id), "cart_subtotal": subtotal,
            "note": payments.ESTIMATE_NOTE, "secure_note": payments.SECURE_NOTE}


@app.post("/api/cart/payment")
def set_payment(body: PaymentIn, request: Request):
    """Choose how to pay for this branch's cart. Refused (409) if the branch doesn't offer it right now."""
    _, owner, guest_id = cart_owner(request, mint=True)
    if not limiter.allow(f"ip:{client_ip(request)}"):
        raise HTTPException(429, "too_many_requests")
    require_branch(body.branch_id)
    if not any(o["code"] == body.method for o in payments.options(payment_config(body.branch_id))):
        raise HTTPException(409, "payment_method_unavailable")
    store_call(cart_store().set_payment, owner, body.branch_id, body.method)
    lines = store_call(cart_store().get, owner, body.branch_id)
    return {"guest_id": guest_id, "payment_method": body.method, "cart": priced(lines, body.branch_id, body.method)}


# ---------- order: review, then an explicit confirm hands the cart to the order backend ----------

class ConfirmIn(BaseModel):
    branch_id: str = Field(..., max_length=40)
    confirm_token: str = Field(..., max_length=120)


def _review(user_id: Optional[str], owner: Optional[str], branch_id: str) -> tuple:
    lines = store_call(cart_store().get, owner, branch_id) if owner else []
    method = owner_payment(owner, branch_id)
    result = checkout.review(user_id=user_id, branch_status=db_retry(repo.get_branch_status, branch_id),
                             cart=db_retry(price_cart, lines, branch_id) if lines else None,
                             payment_config=payment_config(branch_id), payment_code=method)
    return result, lines, method


@app.post("/api/checkout/review")
def review_order(body: BranchIn, request: Request):
    """Is this branch's cart ready to order? problems[] says what to fix; when ready, confirm_token is valid
    for 15 minutes for exactly this cart and payment choice."""
    user_id, owner, guest_id = cart_owner(request, mint=False)
    require_branch(body.branch_id)
    result, lines, method = _review(user_id, owner, body.branch_id)
    token = (checkout.make_token(checkout.fingerprint(owner, body.branch_id, lines, method))
             if result["ready"] else None)
    return {"guest_id": guest_id, **result, "confirm_token": token}


@app.post("/api/checkout/confirm")
def confirm_order(body: ConfirmIn, request: Request):
    """The customer tapped Confirm. Re-checks everything, then hands the cart to the order backend.
    status: placed (cart emptied) | not_connected (complete in the app) | rejected (message) | failed (retry)."""
    user_id, owner, _ = cart_owner(request, mint=False)
    if not limiter.allow(f"ip:{client_ip(request)}"):
        raise HTTPException(429, "too_many_requests")
    require_branch(body.branch_id)
    if not user_id:
        raise HTTPException(401, "login_required")
    carts = cart_store()
    try:
        with carts.lock(owner, body.branch_id):
            result, lines, method = _review(user_id, owner, body.branch_id)
            stale = checkout.check_token(body.confirm_token, checkout.fingerprint(owner, body.branch_id, lines, method))
            if stale:
                raise HTTPException(409, stale)  # "cart_changed" or "expired": review again
            if not result["ready"]:
                raise HTTPException(409, {"code": "not_ready", "problems": result["problems"]})
            outcome = checkout.submit(checkout.order_request(
                user_id=user_id, branch_id=body.branch_id, lines=lines,
                payment_method=result["summary"]["payment_method"], token=body.confirm_token),
                request.headers.get("authorization"))
            if outcome["status"] == checkout.PLACED:
                carts.set(owner, body.branch_id, [])
                carts.set_payment(owner, body.branch_id, None)
                lines = []
    except CartBusy:
        raise HTTPException(409, "cart_busy")
    except CartUnavailable:
        raise HTTPException(503, "cart_unavailable")
    return {**outcome, "cart": priced(lines, body.branch_id, method)}


# ---------- local dev login (DEV_LOGIN=true only) ----------
# Stands in for the main app's login while everything runs locally: the demo page picks a customer and gets
# a token signed exactly like a real one, so the normal JWT path is what gets tested.

DEV_TOKEN_HOURS = 8


def require_dev() -> None:
    if not config.DEV_LOGIN:
        raise HTTPException(404, "Not Found")
    if not config.JWT_SECRET:
        raise HTTPException(400, "dev_login_needs_JWT_SECRET")


class DevLoginIn(BaseModel):
    customer_id: str = Field(..., min_length=1, max_length=64)


@app.get("/api/dev/customers")
def dev_customers():
    """A few customer ids that have orders, to log in as. Ids only: no names or phone numbers."""
    require_dev()

    def recent():
        rows = repo.order_db().orders.aggregate([
            {"$match": {"customerId": {"$nin": [None, ""]}}}, {"$sort": {"createdAt": -1}}, {"$limit": 500},
            {"$group": {"_id": "$customerId", "orders": {"$sum": 1}, "last": {"$max": "$createdAt"}}},
            {"$sort": {"last": -1}}, {"$limit": 8}], maxTimeMS=repo.QUERY_TIMEOUT_MS)
        return [{"customer_id": r["_id"], "orders": r["orders"], "last_order": r["last"]} for r in rows]

    return {"customers": db_retry(recent)}


@app.post("/api/dev/login")
def dev_login(body: DevLoginIn):
    require_dev()
    payload = {"sub": body.customer_id.strip(), "exp": int(time.time()) + DEV_TOKEN_HOURS * 3600, "dev": True}
    if config.JWT_AUDIENCE:
        payload["aud"] = config.JWT_AUDIENCE
    if config.JWT_ISSUER:
        payload["iss"] = config.JWT_ISSUER
    return {"token": pyjwt.encode(payload, config.JWT_SECRET, algorithm="HS256"), "expires_in_h": DEV_TOKEN_HOURS}


# The built widget script (widget/dist) is served from the same origin when present.
if WIDGET_DIR.exists():
    app.mount("/widget", StaticFiles(directory=WIDGET_DIR, html=True), name="widget")
