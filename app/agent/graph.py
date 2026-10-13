"""LangGraph agent (spec 10):

  START -> pre_checks -> agent <-> tools
                           |
                           +-> post_checks --(guard failed, 1 retry)--> agent
                                    |
                                    +-> format_response -> END

Identity: user_id / branch_id are read from config["configurable"] (set by the server / UI), never from
the model. Memory: RedisSaver (or InMemorySaver in dev) keyed by thread_id; each turn's tool traffic is removed at the end so
only question + answer stay in history (spec: no sensitive tool output in long-term memory).
"""
import json
import time
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from groq import RateLimitError
from langchain_core.messages import (AIMessage, AnyMessage, HumanMessage, RemoveMessage, SystemMessage,
                                     ToolMessage)
from langchain_core.runnables import RunnableConfig
from langchain_groq import ChatGroq
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app import config as settings
from app.agent.guards import (LANGUAGE_HINTS, MAX_MESSAGE_CHARS, check_answer, detect_language, pre_check,
                               redact_obj)
from app.data import repositories as repo
from app.retrieval import index
from app.cart import price_cart
from app.tools import RequestContext, run_tool_with_cart, tool_schemas, utc_now_iso

TIME_BUDGET_S = 20
MAX_RATE_LIMIT_WAIT_S = 8
SAFE_FALLBACK = ("Sorry, I couldn't confirm that from our live menu just now. "
                 "Please check the menu screen in the app, or ask me again in a moment.")
TRACE_FILE = settings.ROOT / "logs" / "traces.jsonl"
LLM_ONLY_DROP = {"image_url", "currency", "sold_at_this_branch", "is_deal", "as_of"}


class State(TypedDict, total=False):
    messages: Annotated[List[AnyMessage], add_messages]  # persisted conversation
    cart: List[dict]  # persisted cart lines {item_id, name, size, qty}; never submitted anywhere
    # per-turn scratch (reset in pre_checks)
    turn_id: str
    language: str
    started_at: float
    tool_results: List[dict]
    tools_called: List[str]
    steps: List[dict]
    retried: bool
    answer: Optional[str]
    offline: bool
    response: Dict[str, Any]


def _ctx(config: RunnableConfig) -> RequestContext:
    c = config.get("configurable", {})
    return RequestContext(user_id=c.get("user_id"), branch_id=c["branch_id"])


def _turn_messages(state: State) -> List[AnyMessage]:
    """Messages from this turn's HumanMessage onward."""
    msgs = state["messages"]
    for i, m in enumerate(msgs):
        if m.id == state["turn_id"]:
            return msgs[i:]
    return msgs[-1:]


def _history(state: State) -> List[AnyMessage]:
    msgs = state["messages"]
    for i, m in enumerate(msgs):
        if m.id == state["turn_id"]:
            return msgs[:i][-2 * settings.HISTORY_TURNS:]
    return []


# ---------- LLM ----------

@lru_cache(maxsize=4)
def _llm(model: str) -> ChatGroq:
    kwargs = dict(model=model, api_key=settings.GROQ_API_KEY, temperature=settings.TEMPERATURE,
                  max_tokens=1024, max_retries=0, timeout=20)
    if model.startswith("openai/gpt-oss"):
        kwargs["reasoning_effort"] = "low"  # faster; answer length is kept short by the prompt
    return ChatGroq(**kwargs)


def _retry_after(e: RateLimitError) -> float:
    try:
        return float(e.response.headers.get("retry-after", 60))
    except (TypeError, ValueError, AttributeError):
        return 60.0


def _invoke_llm(messages: List[AnyMessage], allow_tools: bool) -> AIMessage:
    """Main model, then fallbacks when rate limited (each Groq model has its own free-tier quota);
    if all are limited but one frees up within a few seconds, wait once and retry."""
    def attempt():
        last = None
        for model in [settings.GROQ_MODEL] + settings.GROQ_FALLBACK_MODELS:
            llm = _llm(model).bind_tools(tool_schemas()) if allow_tools else _llm(model)
            try:
                return llm.invoke(messages)
            except RateLimitError as e:
                if last is None or _retry_after(e) < _retry_after(last):
                    last = e
        raise last

    try:
        return attempt()
    except RateLimitError as e:
        if _retry_after(e) > MAX_RATE_LIMIT_WAIT_S:
            raise
        time.sleep(_retry_after(e))
        return attempt()


def _system_prompt(ctx: RequestContext) -> str:
    branch = repo.get_branch_status(ctx.branch_id) or {}
    tmpl = (settings.ROOT / "app" / "agent" / "prompts" / "system.md").read_text()
    return tmpl.format(branch_name=branch.get("branch", "unknown"), branch_city=branch.get("city", ""),
                       logged_in="yes" if ctx.user_id else "no (guest; order lookups unavailable)",
                       now=datetime.now(repo.PK_TZ).strftime("%A %d %B %Y, %I:%M %p"))


def _for_llm(result: dict) -> str:
    """Compact JSON for the model (free-tier token limits); drops fields only the app UI needs."""
    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items() if k not in LLM_ONLY_DROP and v not in (None, [], "")}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x
    return json.dumps(strip(result), ensure_ascii=False, default=str, separators=(",", ":"))


# ---------- nodes ----------

def pre_checks(state: State) -> dict:
    human = state["messages"][-1]
    cleaned = pre_check(human.content if isinstance(human.content, str) else "")
    base = {"turn_id": human.id, "started_at": time.time(), "tool_results": [], "tools_called": [],
            "steps": [], "retried": False, "answer": None, "offline": False}
    if cleaned is None:
        return {**base, "answer": f"Please send a message under {MAX_MESSAGE_CHARS} characters."}
    return {**base, "language": detect_language(cleaned),
            "messages": [HumanMessage(content=cleaned, id=human.id)]}  # same id -> replaced in place


def agent(state: State, config: RunnableConfig) -> dict:
    ctx = _ctx(config)
    prompt = ([SystemMessage(_system_prompt(ctx))] + _history(state)
              + [SystemMessage(LANGUAGE_HINTS[state["language"]])]
              + _turn_messages(state))
    try:
        msg = _invoke_llm(prompt, allow_tools=len(state["tools_called"]) < settings.MAX_TOOL_CALLS)
    except Exception as e:  # noqa: BLE001 - LLM down / timeout / quota: fall back, never crash
        return {"offline": True, "steps": state["steps"] + [{"error": type(e).__name__}]}
    return {"messages": [msg]}


def tools(state: State, config: RunnableConfig) -> dict:
    ctx = _ctx(config)
    called, results, steps = list(state["tools_called"]), list(state["tool_results"]), list(state["steps"])
    cart_lines = list(state.get("cart") or [])
    out = []
    for call in state["messages"][-1].tool_calls:
        if len(called) >= settings.MAX_TOOL_CALLS:
            result = {"error": "tool_call_limit_reached"}
        else:
            result, cart_lines = run_tool_with_cart(ctx, call["name"], json.dumps(call["args"]), cart_lines)
            called.append(call["name"])
            results.append(result)
        steps.append({"tool": call["name"], "args": call["args"], "result": result})
        out.append(ToolMessage(content=_for_llm(result), tool_call_id=call["id"], name=call["name"]))
    return {"messages": out, "tools_called": called, "tool_results": results, "steps": steps, "cart": cart_lines}


def post_checks(state: State) -> dict:
    """Price + grounding guard. One retry with feedback, then a safe fallback."""
    draft = (state["messages"][-1].content or "").strip()
    turn = _turn_messages(state)
    problems = check_answer(draft, state["tool_results"], state["tools_called"], turn[0].content)
    steps = state["steps"] + [{"draft": draft, "guard_problems": problems}]
    if not problems:
        return {"answer": draft, "steps": steps}
    if state["retried"] or time.time() - state["started_at"] > TIME_BUDGET_S:
        return {"answer": SAFE_FALLBACK, "steps": steps}
    feedback = HumanMessage("[system check] Your reply failed verification: " + "; ".join(problems)
                            + ". Call the right tool if needed and answer again using only facts from "
                              "tool results.")
    return {"retried": True, "steps": steps, "messages": [feedback]}


def format_response(state: State, config: RunnableConfig) -> dict:
    ctx = _ctx(config)
    turn = _turn_messages(state)
    question = turn[0].content
    if state.get("offline"):
        response = _offline_response(ctx, question, state)
    else:
        answer = state["answer"]
        response = {"answer": answer, "items": _cards(answer, state["tool_results"]),
                    "suggestions": _suggestions(state["tools_called"]), "handoff": False, "as_of": utc_now_iso()}
    try:
        response["cart"] = price_cart(state.get("cart") or [], ctx.branch_id)
    except Exception:  # noqa: BLE001 - DB hiccup: show the answer without the cart panel
        response["cart"] = None
    latency = round(time.time() - state["started_at"], 2)
    response["debug"] = {"tools": state["tools_called"], "latency_s": latency, "guard_retry": state["retried"],
                         "offline": bool(state.get("offline"))}
    _trace({"at": response["as_of"], "user_id": ctx.user_id, "branch_id": ctx.branch_id, "message": question,
            "steps": state["steps"], "answer": response["answer"], "latency_s": latency})
    # Keep only question + final answer in memory; drop tool calls/results and guard feedback.
    removals = [RemoveMessage(id=m.id) for m in turn[1:]]
    return {"response": response, "messages": removals + [AIMessage(content=response["answer"])]}


# ---------- routing ----------

def route_after_pre_checks(state: State) -> str:
    return "format_response" if state.get("answer") else "agent"


def route_after_agent(state: State) -> str:
    if state.get("offline"):
        return "format_response"
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None) and time.time() - state["started_at"] < TIME_BUDGET_S:
        return "tools"
    return "post_checks"


def route_after_post_checks(state: State) -> str:
    return "format_response" if state.get("answer") else "agent"


# ---------- helpers ----------

def _cards(answer: str, results: List[dict]) -> List[dict]:
    """Item cards for the app UI: items from this turn's tool results that the answer mentions."""
    seen, cards = set(), []
    for r in results:
        for it in (r.get("items") or []) + (r.get("deals") or []) + ([r["item"]] if r.get("item") else []):
            key = (it["name"].lower(), it.get("price"), it.get("price_from"))
            if key in seen or it["name"].lower() not in answer.lower():
                continue
            seen.add(key)
            cards.append({k: it.get(k) for k in ("item_id", "name", "category", "price", "price_from", "sizes",
                                                 "currency", "available", "image_url")})
    return cards[:5]


def _suggestions(called: List[str]) -> List[str]:
    if any(c in called for c in ("add_to_cart", "remove_from_cart", "view_cart")):
        return ["Show my cart", "Add a drink", "Show deals"]
    if "get_order_status" in called or "get_order_history" in called:
        return ["Show my past orders", "Show deals"]
    if "get_deals" in called:
        return ["Is my branch open?", "Something spicy"]
    return ["Show deals", "Is my branch open?"]


CART_TOOLS = {"add_to_cart", "remove_from_cart", "clear_cart"}


def _offline_response(ctx: RequestContext, message: str, state: State) -> dict:
    """LLM unavailable: plain menu search so the app is not dead (spec 10, fallback)."""
    if CART_TOOLS & set(state.get("tools_called") or []):
        # The cart already changed this turn: confirm it from code instead of saying "busy".
        cart = price_cart(state.get("cart") or [], ctx.branch_id)
        return {"answer": f"Done, your cart is updated. Subtotal: Rs {cart['subtotal']} "
                          f"(tax and delivery are added in the app at checkout).",
                "items": [], "suggestions": ["Show my cart"], "handoff": False, "as_of": utc_now_iso()}
    try:
        hits = index().search(message, "menu", k=3, branch_id=ctx.branch_id)
        items = [i for i in repo.get_items([h["item_id"] for h in hits], ctx.branch_id) if i["available"]]
    except Exception:  # noqa: BLE001
        items = []
    text = "Our assistant is busy right now. " + (
        "These menu items may match what you asked:" if items else "Please try again in a minute.")
    return {"answer": text, "items": items, "suggestions": [], "handoff": False, "as_of": utc_now_iso()}


def _trace(record: dict) -> None:
    TRACE_FILE.parent.mkdir(exist_ok=True)
    with TRACE_FILE.open("a") as f:
        f.write(json.dumps(redact_obj(record), ensure_ascii=False, default=str) + "\n")


# ---------- graph ----------

def build_graph(checkpointer=None):
    g = StateGraph(State)
    g.add_node("pre_checks", pre_checks)
    g.add_node("agent", agent)
    g.add_node("tools", tools)
    g.add_node("post_checks", post_checks)
    g.add_node("format_response", format_response)
    g.add_edge(START, "pre_checks")
    g.add_conditional_edges("pre_checks", route_after_pre_checks, ["agent", "format_response"])
    g.add_conditional_edges("agent", route_after_agent, ["tools", "post_checks", "format_response"])
    g.add_edge("tools", "agent")
    g.add_conditional_edges("post_checks", route_after_post_checks, ["agent", "format_response"])
    g.add_edge("format_response", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())


def _checkpointer():
    """Redis when REDIS_URL is set (survives restarts, shared by workers); else in-process memory."""
    if not settings.REDIS_URL:
        return InMemorySaver()
    from langgraph.checkpoint.redis import RedisSaver
    saver = RedisSaver(settings.REDIS_URL,
                       ttl={"default_ttl": settings.SESSION_TTL_MIN, "refresh_on_read": True})
    saver.setup()
    return saver


@lru_cache(maxsize=1)
def graph():
    return build_graph(_checkpointer())


def chat(message: str, *, thread_id: str, branch_id: str, user_id: Optional[str] = None) -> dict:
    """One customer turn. user_id must come from verified auth, never from the model or the message."""
    out = graph().invoke(
        {"messages": [HumanMessage(content=message)]},
        config={"configurable": {"thread_id": thread_id, "user_id": user_id, "branch_id": branch_id},
                "recursion_limit": 25},
    )
    return out["response"]
