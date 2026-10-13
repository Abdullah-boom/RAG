"""HTTP API for the website chat widget.  Run: make api   (or uvicorn app.api:app)

Guest-only for now: user_id is always None, so order tools answer "please log in". When the website has
login, verify its JWT here and pass the customer id to chat(); never accept it from the request body.

A conversation is keyed by (branch_id, session_id). The server mints session_id; the widget only stores
it. Switching branch therefore starts a fresh conversation and cart (prices differ per branch).
"""
import re
import threading
import time
import uuid
import warnings
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Deque, Dict, List, Optional

warnings.filterwarnings("ignore")

from fastapi import FastAPI, HTTPException, Query, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app import config  # noqa: E402
from app.agent.graph import chat, graph  # noqa: E402
from app.agent.guards import MAX_MESSAGE_CHARS  # noqa: E402
from app.cart import price_cart  # noqa: E402
from app.data import repositories as repo  # noqa: E402
from app.retrieval import index  # noqa: E402

SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
BRANCH_CACHE_S = 300
WIDGET_DIR = config.ROOT / "widget" / "dist"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    index().search("pizza", "menu", k=1)  # load the embedding model before the first customer
    graph()  # connect the checkpointer (Redis) at startup, not on the first message
    yield


# Interactive docs only when API_DEBUG=true; production exposes just the routes below.
app = FastAPI(title="Cheezious Assistant API", version="1.0", lifespan=lifespan, redoc_url=None,
              docs_url="/api/docs" if config.API_DEBUG else None,
              openapi_url="/api/openapi.json" if config.API_DEBUG else None)
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["GET", "POST"],
                   allow_headers=["Content-Type"], allow_credentials=False, max_age=600)


# ---------- helpers ----------

_branches: Dict[str, object] = {"at": 0.0, "rows": []}
_branch_lock = threading.Lock()


def branches() -> List[dict]:
    with _branch_lock:
        if time.time() - _branches["at"] > BRANCH_CACHE_S or not _branches["rows"]:
            rows = [b for b in repo.list_branches() if not repo.TEST_NAME.search(b["name"])]  # dev test branches
            _branches["rows"] = sorted(rows, key=lambda b: ((b["city"] or ""), b["name"]))
            _branches["at"] = time.time()
        return _branches["rows"]


def require_branch(branch_id: str) -> dict:
    b = next((b for b in branches() if b["branch_id"] == branch_id), None)
    if not b:
        raise HTTPException(404, "unknown_branch")
    return b


def thread_config(branch_id: str, session_id: str) -> dict:
    return {"configurable": {"thread_id": f"{branch_id}:{session_id}"}}


def valid_session(session_id: Optional[str]) -> Optional[str]:
    return session_id if session_id and SESSION_ID.match(session_id) else None


class RateLimiter:
    """Sliding one-minute window per client IP. In-process: with several workers each counts separately."""

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


class SessionIn(BaseModel):
    branch_id: str = Field(..., max_length=40)
    session_id: str = Field(..., max_length=64)


# ---------- routes ----------

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/branches")
def list_branches():
    return {"branches": branches()}


@app.get("/api/branches/{branch_id}/status")
def branch_status(branch_id: str):
    require_branch(branch_id)
    return repo.get_branch_status(branch_id)


@app.post("/api/chat")
def post_chat(body: ChatIn, request: Request):
    if not limiter.allow(client_ip(request)):
        raise HTTPException(429, "too_many_messages")
    require_branch(body.branch_id)
    session_id = valid_session(body.session_id) or uuid.uuid4().hex
    # thread_id carries the branch, so one session id can never mix two branches' carts.
    response = chat(body.message.strip(), thread_id=f"{body.branch_id}:{session_id}",
                    branch_id=body.branch_id, user_id=None)
    out = {k: response.get(k) for k in ("answer", "items", "suggestions", "cart", "handoff", "as_of")}
    if config.API_DEBUG:
        out["debug"] = response.get("debug")
    return {"session_id": session_id, **out}


@app.get("/api/session")
def get_session(branch_id: str = Query(..., max_length=40), session_id: str = Query(..., max_length=64)):
    """Restore a conversation after a page reload: question/answer text + the live-priced cart."""
    require_branch(branch_id)
    if not valid_session(session_id):
        raise HTTPException(404, "unknown_session")
    state = graph().get_state(thread_config(branch_id, session_id)).values
    if not state:
        raise HTTPException(404, "unknown_session")
    messages = [{"role": "user" if isinstance(m, HumanMessage) else "assistant", "text": m.content}
                for m in state.get("messages", []) if isinstance(m, (HumanMessage, AIMessage)) and m.content]
    cart = state.get("cart") or []
    return {"session_id": session_id, "messages": messages,
            "cart": price_cart(cart, branch_id) if cart else None}


@app.post("/api/cart/clear")
def clear_cart(body: SessionIn):
    require_branch(body.branch_id)
    if not valid_session(body.session_id):
        raise HTTPException(404, "unknown_session")
    cfg = thread_config(body.branch_id, body.session_id)
    if graph().get_state(cfg).values:
        graph().update_state(cfg, {"cart": []})
    return {"cart": None}


# The built widget script (widget/dist) is served from the same origin when present.
if WIDGET_DIR.exists():
    app.mount("/widget", StaticFiles(directory=WIDGET_DIR, html=True), name="widget")
