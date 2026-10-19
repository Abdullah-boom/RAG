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

Order type: the first thing the widget asks is delivery or pickup (POST /api/order/type). Delivery sends the
device's location and gets the branch whose delivery area (product.polygons) contains it; pickup sends a branch
picked from the open branches that offer pickup. The choice is stored per cart (with the delivery spot), shown
to the customer like the cart (`order` in responses), and can be switched in chat ("pickup kar do"): the bot's
switch comes back as `action` when the app must ask for a location or a pickup branch first. A switch that
changes branch brings the cart along when the new branch's cart is empty.

Payment: the customer tells the bot how they'll pay (it lists the methods that fit the order type: no cash on
delivery for a pickup) or uses POST /api/cart/payment. It is validated against the branch's live switches and
shown with an estimated tax and total (tax differs by method). Nothing is charged here and card details never
enter the chat: the app's checkout takes the payment.

Ordering: the model never places orders. When the customer asks to order and the cart is ready, the bot shows a
fixed summary and asks for a "yes"; the API keeps a confirm token for that chat session, and the customer's
next message, if it is a plain yes, hands the order (items, order type, delivery spot, payment code) to the
order backend (ORDER_BACKEND_URL) from code. The backend answers with its order id: kept server side
(cart_store.add_placed) so "where is my order?" tracks it in the orders collection; never sent to the app.
POST /api/checkout/review + /api/checkout/confirm do the same for an app with its own Confirm button.

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
from datetime import datetime, timezone  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from pymongo.errors import PyMongoError  # noqa: E402

from app import config  # noqa: E402
from app.agent.graph import chat, chat_stream, graph  # noqa: E402
from app.agent.guards import MAX_MESSAGE_CHARS  # noqa: E402
from app.auth import user_from_request  # noqa: E402
from app import cart as cart_mod  # noqa: E402
from app import locator  # noqa: E402
from app import checkout, order_flow, payments  # noqa: E402
from app.agent.guards import detect_language  # noqa: E402
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
            "accepting_orders": b["accepting_orders"], "pickup": b.get("pickup", True)}


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


def with_checkout(cart: Optional[dict], branch_id: str, method: Optional[str],
                  order_type: Optional[str] = None) -> Optional[dict]:
    """Add the chosen payment method and its tax/total estimate to a priced cart."""
    if not cart:
        return cart
    return {**cart, "checkout": payments.checkout(payment_config(branch_id), cart["subtotal"], method, order_type)}


def owner_payment(owner: Optional[str], branch_id: str) -> Optional[str]:
    return store_call(cart_store().get_payment, owner, branch_id) if owner else None


def owner_order(owner: Optional[str], branch_id: str) -> Optional[dict]:
    return store_call(cart_store().get_order, owner, branch_id) if owner else None


def order_type_of(order: Optional[dict]) -> Optional[str]:
    return (order or {}).get("type")


def priced(lines: list, branch_id: str, method: Optional[str] = None, order_type: Optional[str] = None) -> Optional[dict]:
    return with_checkout(db_retry(price_cart, lines, branch_id), branch_id, method, order_type) if lines else None


def order_view(order: Optional[dict], method: Optional[str]) -> dict:
    """What the customer sees of their order choices, like the cart: type and payment method. Never the
    delivery coordinates or a placed order's id."""
    kind = order_type_of(order)
    return {"type": kind, "type_name": payments.ORDER_TYPE_NAMES.get(kind),
            "payment_method": {"code": method, "name": payments.METHODS[method]["name"]}
            if method in payments.METHODS else None}


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


class OrderTypeIn(BaseModel):
    order_type: str = Field(..., pattern="^(DELIVERY|PICKUP)$")
    branch_id: Optional[str] = Field(None, max_length=40, description="PICKUP: the branch picked from the list")
    lat: Optional[float] = Field(None, ge=-90, le=90, description="DELIVERY: the device's location")
    lng: Optional[float] = Field(None, ge=-180, le=180)
    from_branch_id: Optional[str] = Field(None, max_length=40,
                                          description="The branch the customer was on: its cart comes along")


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
    method, order = owner_payment(owner, body.branch_id), owner_order(owner, body.branch_id)
    return {"branch": public_branch(require_branch(body.branch_id)), "status": status, "guest_id": guest_id,
            "cart": priced(lines, body.branch_id, method, order_type_of(order)), "order": order_view(order, method)}


@app.post("/api/order/type")
def set_order_type(body: OrderTypeIn, request: Request):
    """Delivery or pickup, and so the branch. DELIVERY needs lat/lng: the branch is the open one whose delivery
    area contains the spot (409 no_coverage / branch_closed, with `covering`, when none). PICKUP needs a
    branch_id that is open now and offers pickup (409 branch_closed / pickup_not_offered). from_branch_id: the
    branch the customer is leaving; its cart moves along when the new branch's cart is empty. A payment method
    that doesn't fit the new type is cleared."""
    if not limiter.allow(f"ip:{client_ip(request)}"):
        raise HTTPException(429, "too_many_requests")
    if body.order_type == payments.DELIVERY:
        if body.lat is None or body.lng is None:
            raise HTTPException(422, "location_required")
        rows = branches()
        found = locator.choose_branch(body.lat, body.lng, db_retry(repo.branches_covering, body.lat, body.lng), rows)
        if not found["branch_id"]:
            by_id = {b["branch_id"]: b for b in rows}
            raise HTTPException(409, {"code": found["reason"], "covering": [public_branch(by_id[i])
                                                                             for i in found["covering"]]})
        branch_id, order = found["branch_id"], {"type": payments.DELIVERY, "lat": body.lat, "lng": body.lng}
    else:
        if not body.branch_id:
            raise HTTPException(422, "branch_required")
        branch_id, order = body.branch_id, {"type": payments.PICKUP}
    require_branch(branch_id)
    status = db_retry(repo.get_branch_status, branch_id)
    if not status or not status["open_now"]:
        raise HTTPException(409, {"code": "branch_closed"})
    if order["type"] == payments.PICKUP and not status.get("pickup", True):
        raise HTTPException(409, {"code": "pickup_not_offered"})
    _, owner, guest_id = cart_owner(request, mint=True)
    carts = cart_store()
    moved = False
    try:
        with carts.lock(owner, branch_id):
            lines = carts.get(owner, branch_id)
            method = carts.get_payment(owner, branch_id)
            source = body.from_branch_id if body.from_branch_id and body.from_branch_id != branch_id else None
            if source and not lines and any(b["branch_id"] == source for b in branches()):
                old = carts.get(owner, source)
                if old:  # the customer's cart follows them; it is re-priced (and checked) at the new branch
                    carts.set(owner, branch_id, old)
                    carts.set(owner, source, [])
                    lines, moved = old, True
                    method = method or carts.get_payment(owner, source)
            if method and not any(o["code"] == method
                                  for o in payments.options(payment_config(branch_id), order_type=order["type"])):
                method = None  # cash on delivery after switching to pickup, or not offered at this branch
            carts.set_payment(owner, branch_id, method)
            carts.set_order(owner, branch_id, order)
            carts.set_confirm(owner, branch_id, None)  # a summary shown before the switch no longer holds
    except CartBusy:
        raise HTTPException(409, "cart_busy")
    except CartUnavailable:
        raise HTTPException(503, "cart_unavailable")
    return {"branch": public_branch(require_branch(branch_id)), "status": status, "guest_id": guest_id,
            "cart": priced(lines, branch_id, method, order["type"]), "order": order_view(order, method),
            "cart_moved": moved}


@app.get("/api/branches/{branch_id}/status")
def branch_status(branch_id: str):
    require_branch(branch_id)
    return db_retry(repo.get_branch_status, branch_id)


def public_response(response: dict, session_id: str, guest_id: Optional[str]) -> dict:
    out = {k: response.get(k) for k in ("answer", "items", "suggestions", "cart", "handoff", "as_of", "order",
                                        "action")}
    out["confirm"] = bool(response.get("confirm"))  # this answer is an order summary: show Confirm / Cancel
    if config.API_DEBUG:
        out["debug"] = response.get("debug")
    return {"session_id": session_id, "guest_id": guest_id, **out}


# What the app must do before a switch the customer asked for in chat can happen.
ACTIONS = {"location": "need_location", "pickup_branch": "pick_pickup_branch"}


def save_turn_cart(owner: str, branch_id: str, response: dict, method: Optional[str], order: Optional[dict],
                   session_id: str) -> dict:
    """Save what the turn changed (cart, payment choice, delivery / pickup) and return the response with the
    checkout estimate and the order as the customer sees it. A ready review keeps a confirm token for this
    session: the customer's next "yes" places exactly that order (confirm_turn)."""
    carts = cart_store()
    if "cart_lines" in response:
        carts.set(owner, branch_id, response["cart_lines"])
    if response.get("payment_method_set"):
        method = response["payment_method_set"]
        carts.set_payment(owner, branch_id, method)
    action = None
    switch = response.get("order_type_set")
    if switch and switch.get("needs"):
        action = {"type": ACTIONS[switch["needs"]], "order_type": switch["order_type_selected"]}
    elif switch and switch["order_type_selected"] != order_type_of(order):
        kind = switch["order_type_selected"]
        # Delivery is only set here when the tool found the stored spot inside this branch's delivery area.
        order = {"type": kind, **({"lat": order["lat"], "lng": order["lng"]}
                                  if kind == payments.DELIVERY and order and order.get("lat") is not None else {})}
        carts.set_order(owner, branch_id, order)
        if method and not payments.fits(method, kind):
            method = None
            carts.set_payment(owner, branch_id, None)
    lines = response["cart_lines"] if "cart_lines" in response else carts.get(owner, branch_id)
    if response.get("review_ready"):
        token = checkout.make_token(checkout.fingerprint(owner, branch_id, lines, method, order))
        carts.set_confirm(owner, branch_id, {"token": token, "session": session_id,
                                             "lang": response.get("language") or "latin"})
    return {**response, "cart": with_checkout(response.get("cart"), branch_id, method, order_type_of(order)),
            "order": order_view(order, method), "action": action, "confirm": bool(response.get("review_ready"))}


def place_order(user_id: str, owner: str, branch_id: str, token: str, auth: Optional[str]) -> dict:
    """Re-check everything against the confirm token, then hand the order to the backend. Call under the cart
    lock. status: placed | not_connected | rejected | failed (from checkout.submit), or stale / not_ready.
    On placed the cart and payment choice are cleared and the backend's order id is kept for tracking."""
    carts = cart_store()
    result, lines, method, order = _review(user_id, owner, branch_id)
    stale = checkout.check_token(token, checkout.fingerprint(owner, branch_id, lines, method, order))
    if stale:
        return {"status": "stale", "reason": stale, "review": result, "lines": lines, "method": method, "order": order}
    if not result["ready"]:
        return {"status": "not_ready", "review": result, "lines": lines, "method": method, "order": order}
    outcome = checkout.submit(checkout.order_request(
        user_id=user_id, branch_id=branch_id, lines=lines, payment_method=result["summary"]["payment_method"],
        token=token, order=order), auth)
    if outcome["status"] == checkout.PLACED:
        carts.set(owner, branch_id, [])
        carts.set_payment(owner, branch_id, None)
        carts.add_placed(owner, {"order_id": outcome.get("order_id"), "order_no": outcome.get("order_no"),
                                 "branch_id": branch_id, "branch": result["summary"].get("branch"),
                                 "order_type": order_type_of(order),
                                 "placed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()})
        lines, method = [], None
    pay_now = bool((result["summary"]["payment_method"] or {}).get("pay_now"))
    return {**outcome, "lines": lines, "method": method, "order": order, "pay_now": pay_now}


def _reply_lang(message: str) -> str:
    lang = order_flow.answer_language(message, detect_language(message))
    return "roman_urdu" if lang == "mixed" else lang if lang in order_flow.LANGS else "latin"


def confirm_turn(message: str, *, user_id: Optional[str], owner: str, branch_id: str, session_id: str,
                 auth: Optional[str]) -> Optional[dict]:
    """A typed answer to the order summary ("yes", "haan ji", "no"), from code (no model). None: no summary is
    waiting in this session, or the message is something else; then it is a normal chat turn (and the waiting
    summary is dropped, so a later "yes" can't place an order the customer has moved on from)."""
    carts = cart_store()
    pending = carts.get_confirm(owner, branch_id)
    if not pending:
        return None
    carts.set_confirm(owner, branch_id, None)
    answer = order_flow.answer_to_confirm(message)
    if pending.get("session") != session_id or not answer:
        return None
    return resolve_pending(pending, answer, message, _reply_lang(message), user_id=user_id, owner=owner,
                           branch_id=branch_id, session_id=session_id, auth=auth)


def resolve_pending(pending: dict, answer: str, said: str, lang: str, *, user_id: Optional[str], owner: str,
                    branch_id: str, session_id: str, auth: Optional[str]) -> dict:
    """Yes / no to the order summary waiting in this session: the Confirm / Cancel buttons and a typed yes / no
    both end here. Call under the cart lock, with the pending token already taken from the store."""
    carts = cart_store()
    method, order = carts.get_payment(owner, branch_id), carts.get_order(owner, branch_id)
    lines = carts.get(owner, branch_id)
    suggestions = ["Show my cart"]
    confirm = False  # a new summary to confirm (the old one changed)
    if answer == "no":
        text = order_flow.outcome_text("declined", lang)
    elif not user_id:
        text = order_flow.outcome_text("login", lang)
    else:
        r = place_order(user_id, owner, branch_id, pending["token"], auth)
        lines, method, order = r["lines"], r["method"], r["order"]
        if r["status"] in ("stale", "not_ready"):
            if r["review"]["ready"]:  # changed but still fine: show it again and wait for a new yes
                text = order_flow.outcome_text("changed", lang) + "\n\n" + order_flow.summary_text(r["review"], lang)
                token = checkout.make_token(checkout.fingerprint(owner, branch_id, lines, method, order))
                carts.set_confirm(owner, branch_id, {"token": token, "session": session_id, "lang": lang})
                confirm = True
            else:
                text = order_flow.problems_text(r["review"]["problems"], lang)
        elif r["status"] == checkout.PLACED:
            text = order_flow.outcome_text("placed", lang, order_type=order_type_of(order), pay_now=r["pay_now"])
            suggestions = ["Where is my order?"]
        else:
            text = order_flow.outcome_text(r["status"], lang, message=r.get("message"))
            if r["status"] == checkout.FAILED:  # same token: the backend's idempotency key stops a double order
                carts.set_confirm(owner, branch_id, pending)
                confirm = True
    # Into the conversation like any turn, so the model sees it next time ("mera order kahan hai?").
    graph().update_state(thread_config(branch_id, user_id, session_id),
                         {"messages": [HumanMessage(content=said), AIMessage(content=text)]},
                         as_node="format_response")
    cart = priced(lines, branch_id, method, order_type_of(order))
    return {"answer": text, "items": [], "suggestions": suggestions, "handoff": False,
            "as_of": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "cart": cart, "order": order_view(order, method), "action": None, "confirm": confirm}


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
    auth = request.headers.get("authorization")
    confirm_args = dict(user_id=user_id, owner=owner, branch_id=body.branch_id, session_id=session_id, auth=auth)

    def turn_state():
        """(cart, payment, order, tracked) for this turn, read under the cart lock."""
        return (carts.get(owner, body.branch_id), carts.get_payment(owner, body.branch_id),
                carts.get_order(owner, body.branch_id), carts.placed_orders(owner) if user_id else [])

    if body.stream:
        def events():
            # The cart stays locked for the whole turn, so a cart button pressed meanwhile waits for it.
            try:
                with carts.lock(owner, body.branch_id):
                    done = confirm_turn(message, **confirm_args)
                    if done:
                        yield sse("answer", public_response(done, session_id, guest_id))
                        return
                    lines, method, order, tracked = turn_state()
                    for ev in chat_stream(message, cart=lines, payment_method=method, order=order, tracked=tracked,
                                          **args):
                        if "status" in ev:
                            yield sse("status", {"text": ev["status"]})
                        elif ev["response"].get("error") == DB_UNAVAILABLE:
                            yield sse("error", {"code": DB_UNAVAILABLE})
                        else:
                            reply = save_turn_cart(owner, body.branch_id, ev["response"], method, order, session_id)
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
            done = confirm_turn(message, **confirm_args)
            if done:
                return public_response(done, session_id, guest_id)
            lines, method, order, tracked = turn_state()
            response = chat(message, cart=lines, payment_method=method, order=order, tracked=tracked, **args)
            if response.get("error") == DB_UNAVAILABLE:
                raise HTTPException(503, DB_UNAVAILABLE)  # the turn was discarded; the widget offers "Try again"
            response = save_turn_cart(owner, body.branch_id, response, method, order, session_id)
    except CartBusy:
        raise HTTPException(409, "cart_busy")
    except CartUnavailable:
        raise HTTPException(503, "cart_unavailable")
    return public_response(response, session_id, guest_id)


class ChatConfirmIn(BaseModel):
    branch_id: str = Field(..., max_length=40)
    session_id: str = Field(..., max_length=64)
    answer: str = Field(..., pattern="^(yes|no)$")


BUTTON_SAID = {"yes": "Confirm order", "no": "Cancel"}


@app.post("/api/chat/confirm")
def chat_confirm(body: ChatConfirmIn, request: Request):
    """The Confirm order / Cancel buttons under the bot's order summary. The server holds the confirm token for
    this chat session (the app never sees it); this does exactly what a typed "yes" / "no" does, without the
    model. 409 nothing_to_confirm: no summary waiting (answered, expired, or the customer moved on)."""
    user_id, owner, guest_id = cart_owner(request, mint=False)
    if not limiter.allow(f"ip:{client_ip(request)}"):
        raise HTTPException(429, "too_many_requests")
    require_branch(body.branch_id)
    session_id = valid_session(body.session_id)
    if not owner or not session_id:
        raise HTTPException(409, "nothing_to_confirm")
    carts = cart_store()
    try:
        with carts.lock(owner, body.branch_id):
            pending = carts.get_confirm(owner, body.branch_id)
            if not pending or pending.get("session") != session_id:
                raise HTTPException(409, "nothing_to_confirm")
            carts.set_confirm(owner, body.branch_id, None)
            reply = resolve_pending(pending, body.answer, BUTTON_SAID[body.answer], pending.get("lang") or "latin",
                                    user_id=user_id, owner=owner, branch_id=body.branch_id, session_id=session_id,
                                    auth=request.headers.get("authorization"))
    except CartBusy:
        raise HTTPException(409, "cart_busy")
    except CartUnavailable:
        raise HTTPException(503, "cart_unavailable")
    return public_response(reply, session_id, guest_id)


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
    method, order = owner_payment(owner, branch_id), owner_order(owner, branch_id)
    return {"session_id": session_id, "messages": messages,
            "cart": priced(lines, branch_id, method, order_type_of(order)), "order": order_view(order, method)}


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
            kind = order_type_of(carts.get_order(owner, branch_id))
    except CartBusy:
        raise HTTPException(409, "cart_busy")
    except CartUnavailable:
        raise HTTPException(503, "cart_unavailable")
    cart = with_checkout(result.get("cart"), branch_id, method, kind) if new_lines else None
    return {"guest_id": guest_id, **result, "cart": cart}


@app.get("/api/cart")
def get_cart(request: Request, branch_id: str = Query(..., max_length=40)):
    _, owner, guest_id = cart_owner(request, mint=False)
    require_branch(branch_id)
    lines = store_call(cart_store().get, owner, branch_id) if owner else []
    method, order = owner_payment(owner, branch_id), owner_order(owner, branch_id)
    return {"guest_id": guest_id, "cart": priced(lines, branch_id, method, order_type_of(order)),
            "order": order_view(order, method)}


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
    kind = order_type_of(owner_order(owner, branch_id))
    return {"guest_id": guest_id, "order_type": kind,
            "methods": payments.options(payment_config(branch_id), subtotal, kind),
            "chosen": owner_payment(owner, branch_id), "cart_subtotal": subtotal,
            "note": payments.ESTIMATE_NOTE, "secure_note": payments.SECURE_NOTE}


@app.post("/api/cart/payment")
def set_payment(body: PaymentIn, request: Request):
    """Choose how to pay for this branch's cart. Refused (409) if the branch doesn't offer it right now."""
    _, owner, guest_id = cart_owner(request, mint=True)
    if not limiter.allow(f"ip:{client_ip(request)}"):
        raise HTTPException(429, "too_many_requests")
    require_branch(body.branch_id)
    order = owner_order(owner, body.branch_id)
    if not any(o["code"] == body.method for o in payments.options(payment_config(body.branch_id))):
        raise HTTPException(409, "payment_method_unavailable")
    if not payments.fits(body.method, order_type_of(order)):
        raise HTTPException(409, "payment_method_wrong_type")
    store_call(cart_store().set_payment, owner, body.branch_id, body.method)
    lines = store_call(cart_store().get, owner, body.branch_id)
    return {"guest_id": guest_id, "payment_method": body.method,
            "cart": priced(lines, body.branch_id, body.method, order_type_of(order)),
            "order": order_view(order, body.method)}


# ---------- order: review, then an explicit confirm hands the cart to the order backend ----------

class ConfirmIn(BaseModel):
    branch_id: str = Field(..., max_length=40)
    confirm_token: str = Field(..., max_length=120)


def _review(user_id: Optional[str], owner: Optional[str], branch_id: str) -> tuple:
    lines = store_call(cart_store().get, owner, branch_id) if owner else []
    method, order = owner_payment(owner, branch_id), owner_order(owner, branch_id)
    result = checkout.review(user_id=user_id, branch_status=db_retry(repo.get_branch_status, branch_id),
                             cart=db_retry(price_cart, lines, branch_id) if lines else None,
                             payment_config=payment_config(branch_id), payment_code=method, order=order)
    return result, lines, method, order


@app.post("/api/checkout/review")
def review_order(body: BranchIn, request: Request):
    """Is this branch's cart ready to order? problems[] says what to fix; when ready, confirm_token is valid
    for 15 minutes for exactly this cart and payment choice."""
    user_id, owner, guest_id = cart_owner(request, mint=False)
    require_branch(body.branch_id)
    result, lines, method, order = _review(user_id, owner, body.branch_id)
    token = (checkout.make_token(checkout.fingerprint(owner, body.branch_id, lines, method, order))
             if result["ready"] else None)
    return {"guest_id": guest_id, **result, "confirm_token": token}


@app.post("/api/checkout/confirm")
def confirm_order(body: ConfirmIn, request: Request):
    """The customer tapped Confirm. Re-checks everything, then hands the cart to the order backend.
    status: placed (cart emptied) | not_connected (complete in the app) | rejected (message) | failed (retry).
    The backend's order id is kept for the bot's tracking and not returned."""
    user_id, owner, _ = cart_owner(request, mint=False)
    if not limiter.allow(f"ip:{client_ip(request)}"):
        raise HTTPException(429, "too_many_requests")
    require_branch(body.branch_id)
    if not user_id:
        raise HTTPException(401, "login_required")
    carts = cart_store()
    try:
        with carts.lock(owner, body.branch_id):
            r = place_order(user_id, owner, body.branch_id, body.confirm_token, request.headers.get("authorization"))
            if r["status"] == "stale":
                raise HTTPException(409, r["reason"])  # "cart_changed" or "expired": review again
            if r["status"] == "not_ready":
                raise HTTPException(409, {"code": "not_ready", "problems": r["review"]["problems"]})
            carts.set_confirm(owner, body.branch_id, None)
    except CartBusy:
        raise HTTPException(409, "cart_busy")
    except CartUnavailable:
        raise HTTPException(503, "cart_unavailable")
    return {"status": r["status"], "message": r.get("message"),
            "cart": priced(r["lines"], body.branch_id, r["method"], order_type_of(r["order"])),
            "order": order_view(r["order"], r["method"])}


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


@app.post("/api/dev/order-backend")
def dev_order_backend(payload: dict):
    """Stands in for the main backend's order endpoint while it doesn't exist (DEV_LOGIN=true only): point
    ORDER_BACKEND_URL here to try the whole flow. Answers like the real one should: an order id, no order number
    yet. Nothing is written to MongoDB, so tracking reports the order as waiting for the branch."""
    if not config.DEV_LOGIN:
        raise HTTPException(404, "Not Found")
    if not payload.get("items") or payload.get("order_type") not in payments.ORDER_TYPES:
        raise HTTPException(422, {"message": "items and order_type are required"})
    return {"orderId": uuid.uuid4().hex[:24], "status": "PLACED"}


# The built widget script (widget/dist) is served from the same origin when present.
if WIDGET_DIR.exists():
    app.mount("/widget", StaticFiles(directory=WIDGET_DIR, html=True), name="widget")
