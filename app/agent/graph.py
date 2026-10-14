"""LangGraph agent (spec 10):

  START -> pre_checks -> prefetch -> agent <-> tools
                           |
                           +-> post_checks --(guard failed, 1 retry)--> agent
                                    |
                                    +-> format_response -> END

Identity: user_id / branch_id are read from config["configurable"] (set by the server / UI), never from
the model. Memory: RedisSaver (or InMemorySaver in dev) keyed by thread_id; each turn's tool traffic is removed at the end so
only question + answer stay in history (spec: no sensitive tool output in long-term memory).
DB failures: a Mongo read that fails twice (any node) ends the turn with one clear error; the turn is dropped
from memory and its cart changes are undone, so the customer never gets an answer built on partial data.
Cart: the caller passes the branch's cart in (app/cart_store.py owns it, not this thread) and saves
response["cart_lines"] back; the copy in this thread's state is only the working copy for one turn.
"""
import json
import re
import time
from dataclasses import replace
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Any, Dict, Iterator, List, Optional, TypedDict

from groq import RateLimitError
from langchain_core.messages import (AIMessage, AnyMessage, HumanMessage, RemoveMessage, SystemMessage,
                                     ToolMessage)
from langchain_core.runnables import RunnableConfig
from langchain_groq import ChatGroq
from langchain_ollama import ChatOllama
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pymongo.errors import PyMongoError

from app import config as settings
from app.agent.intents import QUESTION, add_call, prefetch_tools
from app.cart import chosen_from_offered, pointed_item
from app.agent.guards import (LANGUAGE_HINTS, MAX_MESSAGE_CHARS, NOT_ROMAN_URDU, REPLY_NOTES, check_answer,
                               detect_language, pre_check, redact_obj)
from app.data import repositories as repo
from app.data.db import with_retry
from app.retrieval import index
from app.cart import price_cart
from app.payments import find_card_number, redact_card_numbers
from app.tools import (DB_UNAVAILABLE, RequestContext, SearchMenuArgs, run_tool_with_cart, search_menu,
                       tool_schemas, utc_now_iso)

TIME_BUDGET_S = settings.LLM_TIME_BUDGET_S
MAX_RATE_LIMIT_WAIT_S = 8
# When no draft passes the guard (the model's answer couldn't be verified against this turn's lookups).
# With items found this turn, those are shown as cards under FOUND_FALLBACKS.
SAFE_FALLBACK = ("Sorry, I couldn't put together a reliable answer to that. Could you ask it another way, "
                 "or check the menu in the app?")
CARD_WARNINGS = {
    "latin": "Please don't share card details in chat. I've removed that number. To pay by card, choose an online "
             "payment method here and pay in the app's secure checkout.",
    "roman_urdu": "Meharbani kar ke chat mein card ki details na bhejein. Maine woh number hata diya hai. Card se pay "
                  "karne ke liye online payment chunein aur app ke secure checkout mein payment karein.",
    "urdu_script": "براہ کرم چیٹ میں کارڈ کی تفصیلات نہ بھیجیں۔ میں نے وہ نمبر ہٹا دیا ہے۔ کارڈ سے ادائیگی کے لیے "
                   "آن لائن ادائیگی منتخب کریں اور ایپ کے محفوظ چیک آؤٹ میں ادائیگی کریں۔",
}
CARD_WARNINGS["mixed"] = CARD_WARNINGS["roman_urdu"]
FALLBACKS = {
    "latin": SAFE_FALLBACK,
    "roman_urdu": "Maaf kijiye, iska pakka jawab nahi bana saka. Thora alag tareeqe se poochein, ya app mein menu "
                  "dekh lein.",
    "urdu_script": "معذرت، اس کا پکا جواب نہیں بن سکا۔ ذرا مختلف طریقے سے پوچھیں، یا ایپ میں مینو دیکھ لیں۔",
}
FOUND_FALLBACKS = {
    "latin": "I couldn't put a full answer together just now, but here's what I found on our live menu:",
    "roman_urdu": "Abhi poora jawab nahi bana saka, lekin live menu se yeh mila hai:",
    "urdu_script": "ابھی پورا جواب نہیں بن سکا، لیکن لائیو مینو سے یہ ملا ہے:",
}
DB_DOWN_ANSWER = ("Sorry, I can't reach our live menu right now, so I can't answer that reliably. "
                  "Please try again in a minute.")
TRACE_FILE = settings.ROOT / "logs" / "traces.jsonl"
# Progress shown to the customer while a turn runs (chat_stream). The answer itself is only sent after
# post_checks, so an unverified price never reaches the screen.
TOOL_STATUS = {
    "search_menu": "Searching the menu…", "get_item_details": "Checking live prices…",
    "get_deals": "Looking up today's deals…", "get_best_sellers": "Finding popular items…",
    "check_branch_status": "Checking branch hours…", "get_order_status": "Looking up your order…",
    "get_order_history": "Looking up your orders…", "search_faq": "Checking our help pages…",
    "calculate_total": "Working out the total…", "add_to_cart": "Updating your cart…",
    "remove_from_cart": "Updating your cart…", "view_cart": "Checking your cart…", "clear_cart": "Clearing your cart…",
    "get_payment_methods": "Checking payment options…", "set_payment_method": "Saving your payment choice…",
    "review_order": "Checking your order…",
}
LLM_ONLY_DROP = {"image_url", "currency", "sold_at_this_branch", "is_deal", "as_of"}


class State(TypedDict, total=False):
    messages: Annotated[List[AnyMessage], add_messages]  # persisted conversation
    cart: List[dict]  # this turn's working copy of the branch cart {item_id, name, size, qty}; never submitted
    offered: List[dict]  # items the last answer showed, in order: what "the first one" / "yeh wala" refers to
    # per-turn scratch (reset in pre_checks)
    turn_id: str
    language: str
    started_at: float
    tool_results: List[dict]
    tools_called: List[str]
    steps: List[dict]
    retried: bool
    answer: Optional[str]
    fallback: bool  # no draft passed the guard: answer is FALLBACKS / FOUND_FALLBACKS, cards = everything found
    offline: bool
    db_error: bool  # a Mongo read failed this turn: answer with one clear error, nothing partial
    cart_before: List[dict]  # restored if the turn fails
    response: Dict[str, Any]


def _ctx(config: RunnableConfig) -> RequestContext:
    c = config.get("configurable", {})
    return RequestContext(user_id=c.get("user_id"), branch_id=c["branch_id"], payment_method=c.get("payment_method"))


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

@lru_cache(maxsize=1)
def _ollama() -> ChatOllama:
    # num_ctx: Ollama's 4k default silently truncates the system prompt + tool schemas (~4.5k tokens).
    # num_predict caps answer length; keep_alive keeps the model loaded between customers.
    return ChatOllama(model=settings.OLLAMA_MODEL, base_url=settings.OLLAMA_BASE_URL,
                      temperature=settings.TEMPERATURE, num_ctx=8192, num_predict=1024, keep_alive="30m",
                      client_kwargs=_ollama_client_kwargs())


def _ollama_client_kwargs() -> dict:
    kwargs = {"timeout": TIME_BUDGET_S, "headers": {"ngrok-skip-browser-warning": "1"}}  # harmless off ngrok
    if settings.OLLAMA_AUTH:
        user, _, password = settings.OLLAMA_AUTH.partition(":")
        kwargs["auth"] = (user, password)
    return kwargs


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
    """Ollama: the local model, no rate limits. Groq: main model, then fallbacks when rate limited (each Groq
    model has its own free-tier quota); if all are limited but one frees up within a few seconds, wait once and retry."""
    if settings.LLM_PROVIDER == "ollama":
        llm = _ollama().bind_tools(tool_schemas()) if allow_tools else _ollama()
        return llm.invoke(messages)

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


@lru_cache(maxsize=1)
def _menu_names() -> tuple:
    """Lower-cased menu item names (from the index) for the grounding guard."""
    return tuple(sorted({re.sub(r"[^0-9a-z]+", " ", d["metadata"]["name"].lower()).strip()
                         for d in index().docs if d["metadata"]["kind"] == "menu"}))


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


def _status(text: str) -> None:
    """Send a progress line to chat_stream listeners; a no-op for plain chat()."""
    try:
        get_stream_writer()({"status": text})
    except Exception:  # noqa: BLE001 - outside a streaming run there is no writer
        pass


# ---------- nodes ----------

def pre_checks(state: State) -> dict:
    human = state["messages"][-1]
    cleaned = pre_check(human.content if isinstance(human.content, str) else "")
    base = {"turn_id": human.id, "started_at": time.time(), "tool_results": [], "tools_called": [],
            "steps": [], "retried": False, "answer": None, "fallback": False, "offline": False, "db_error": False,
            "cart_before": list(state.get("cart") or [])}
    if cleaned is None:
        return {**base, "answer": f"Please send a message under {MAX_MESSAGE_CHARS} characters."}
    if find_card_number(cleaned):
        # Never let a card number reach the model, memory or logs; online payment is the app's checkout.
        language = detect_language(cleaned)
        return {**base, "language": language, "answer": CARD_WARNINGS.get(language, CARD_WARNINGS["latin"]),
                "messages": [HumanMessage(content=redact_card_numbers(cleaned), id=human.id)]}
    return {**base, "language": detect_language(cleaned),
            "messages": [HumanMessage(content=cleaned, id=human.id)]}  # same id -> replaced in place


def agent(state: State, config: RunnableConfig) -> dict:
    ctx = _ctx(config)
    if not state["tools_called"] and not state["retried"]:
        _status("Thinking…")
    try:
        system = with_retry(_system_prompt, ctx)
    except PyMongoError:
        return {"db_error": True, "steps": state["steps"] + [{"error": DB_UNAVAILABLE}]}
    turn = _turn_messages(state)
    note = REPLY_NOTES.get(state["language"])
    if note:  # prompt copy only: memory and the guards keep the customer's own words
        turn = [HumanMessage(content=f"{turn[0].content}\n\n{note}")] + turn[1:]
    prompt = ([SystemMessage(system)] + _history(state)
              + [SystemMessage(LANGUAGE_HINTS[state["language"]] + _offered_note(state))]
              + turn)
    try:
        # After a pre-run add_to_cart the model only reports what the cart code did: no second add.
        allow = len(state["tools_called"]) < settings.MAX_TOOL_CALLS and "add_to_cart" not in state["tools_called"]
        msg = _invoke_llm(prompt, allow_tools=allow)
    except Exception as e:  # noqa: BLE001 - LLM down / timeout / quota: fall back, never crash
        return {"offline": True, "steps": state["steps"] + [{"error": type(e).__name__}]}
    return {"messages": [msg]}


def tools(state: State, config: RunnableConfig) -> dict:
    ctx = replace(_ctx(config), user_message=_turn_messages(state)[0].content,
                  offered=tuple(state.get("offered") or ()))
    called, results, steps = list(state["tools_called"]), list(state["tool_results"]), list(state["steps"])
    cart_lines = list(state.get("cart") or [])
    out = []
    for call in state["messages"][-1].tool_calls:
        if len(called) >= settings.MAX_TOOL_CALLS:
            result = {"error": "tool_call_limit_reached"}
        else:
            _status(TOOL_STATUS.get(call["name"], "Checking…"))
            result, cart_lines = run_tool_with_cart(ctx, call["name"], json.dumps(call["args"]), cart_lines)
            called.append(call["name"])
            results.append(result)
        steps.append({"tool": call["name"], "args": call["args"], "result": result})
        if result.get("error") == DB_UNAVAILABLE:  # the turn ends here; format_response discards it
            return {"db_error": True, "tools_called": called, "steps": steps}
        out.append(ToolMessage(content=_for_llm(result), tool_call_id=call["id"], name=call["name"]))
    return {"messages": out, "tools_called": called, "tool_results": results, "steps": steps, "cart": cart_lines}


def prefetch(state: State, config: RunnableConfig) -> dict:
    """Run the obvious tools (cart, deals, best sellers, branch status, plain price searches) before the
    model writes, as if it had called them: small models otherwise skip them and answer from memory."""
    said = _turn_messages(state)[0].content
    offered = tuple(state.get("offered") or ())
    calls = prefetch_tools(said)
    if not calls and offered:
        pointed = pointed_item(said, offered)
        if pointed and QUESTION.search(said):  # "pehli wali kitne ki hai?": look up the item pointed at
            calls = [("search_menu", {"query": pointed["name"], "picked": True})]
        elif not QUESTION.search(said) and chosen_from_offered(said, offered):
            calls = [add_call(said)]  # replying to "which one?" with a name: that's choosing it
    if not calls:
        return {}
    call = AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": f"prefetch_{i}", "type": "tool_call"}
                                             for i, (n, a) in enumerate(calls)])
    out = tools({**state, "messages": state["messages"] + [call]}, config)
    out["messages"] = [call] + out.get("messages", [])
    return out


def post_checks(state: State) -> dict:
    """Price + grounding guard. One retry with feedback, then a safe fallback."""
    draft = (state["messages"][-1].content or "").strip()
    if not draft:
        # No text to check: the time budget ran out while the model still wanted a tool. Answer safely
        # instead of looping back to the agent (an empty answer used to route there until the recursion limit).
        return {**_fallback(state), "steps": state["steps"] + [{"draft": "", "guard_problems": ["no_answer_in_time"]}]}
    turn = _turn_messages(state)
    problems = check_answer(draft, state["tool_results"], state["tools_called"], turn[0].content, _menu_names())
    steps = state["steps"] + [{"draft": draft, "guard_problems": problems}]
    if not problems:
        return {"answer": draft, "steps": steps}
    if state["retried"] or time.time() - state["started_at"] > TIME_BUDGET_S:
        # A draft whose facts passed and whose only fault was English instead of Roman Urdu beats the
        # fallback. Not an Urdu-script slip: small models garble it ("yہ رہی آپکی کارت").
        usable = [s["draft"] for s in steps if s.get("draft") and s.get("guard_problems") == [NOT_ROMAN_URDU]]
        return {"answer": usable[-1], "steps": steps} if usable else {**_fallback(state), "steps": steps}
    _status("Double-checking the answer…")
    feedback = HumanMessage("[system check] Your reply failed verification: " + "; ".join(problems)
                            + ". Call the right tool if needed and answer again using only facts from "
                              "tool results.")
    return {"retried": True, "steps": steps, "messages": [feedback]}


def format_response(state: State, config: RunnableConfig) -> dict:
    ctx = _ctx(config)
    turn = _turn_messages(state)
    question = turn[0].content
    response = None
    if not state.get("db_error"):
        try:
            if state.get("offline"):
                response = _offline_response(ctx, question, state)
            else:
                answer = state["answer"]
                cards = (_found_cards(state["tool_results"]) if state.get("fallback")
                         else _which_one_cards(state["tool_results"]) or _cards(answer, state["tool_results"]))
                response = {"answer": answer, "items": cards,
                            "suggestions": _suggestions(state["tools_called"]), "handoff": False,
                            "as_of": utc_now_iso()}
            response["cart"] = with_retry(price_cart, state.get("cart") or [], ctx.branch_id)
        except PyMongoError:
            response = None
    failed = response is None
    if failed:  # any Mongo failure this turn: one clear error, never a partial answer
        response = {"answer": DB_DOWN_ANSWER, "items": [], "suggestions": [], "handoff": False,
                    "as_of": utc_now_iso(), "cart": None, "error": DB_UNAVAILABLE}
    latency = round(time.time() - state["started_at"], 2)
    response["debug"] = {"tools": state["tools_called"], "latency_s": latency, "guard_retry": state["retried"],
                         "offline": bool(state.get("offline"))}
    _trace({"at": response["as_of"], "user_id": ctx.user_id, "branch_id": ctx.branch_id, "message": question,
            "steps": state["steps"], "answer": response["answer"], "latency_s": latency})
    if failed:
        # Forget the whole turn and undo its cart changes, so "Try again" starts clean.
        before = state.get("cart_before") or []
        response["cart_lines"] = before
        return {"response": response, "messages": [RemoveMessage(id=m.id) for m in turn], "cart": before}
    response["cart_lines"] = state.get("cart") or []  # for the caller to save; not part of the public reply
    chosen = [r["payment_method_selected"] for r in state.get("tool_results") or [] if r.get("payment_method_selected")]
    if chosen:
        response["payment_method_set"] = chosen[-1]  # for the caller to save with the cart
    # Keep only question + final answer in memory; drop tool calls/results and guard feedback.
    removals = [RemoveMessage(id=m.id) for m in turn[1:]]
    # What "the first one" / "large" / "Crown Crust" refers to next: this answer's cards; with no new cards the
    # last list stays, until the cart changes (then an old list would point at the wrong thing).
    offered = [{"item_id": i["item_id"], "name": i["name"], "type": i.get("type")} for i in response["items"]]
    if not offered and (state.get("cart") or []) == (state.get("cart_before") or []):
        offered = state.get("offered") or []
    return {"response": response, "messages": removals + [AIMessage(content=response["answer"])], "offered": offered}


# ---------- routing ----------

def route_after_pre_checks(state: State) -> str:
    return "format_response" if state.get("answer") else "prefetch"


def route_after_agent(state: State) -> str:
    if state.get("offline") or state.get("db_error"):
        return "format_response"
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None) and time.time() - state["started_at"] < TIME_BUDGET_S:
        return "tools"
    return "post_checks"


def route_after_tools(state: State) -> str:
    return "format_response" if state.get("db_error") else "agent"


def route_after_post_checks(state: State) -> str:
    return "format_response" if state.get("answer") is not None else "agent"


# ---------- helpers ----------

def _offered_note(state: State) -> str:
    """So "the first one" / "pehli wali" means the same to the model as to the cart code."""
    offered = state.get("offered") or []
    if not offered:
        return ""
    listed = "; ".join(f"{n}. {o['name']}" + (f" ({o['type']})" if o.get("type") else "")
                       for n, o in enumerate(offered, 1))
    return (f"\nItems your last answer showed the customer, in order: {listed}. If the customer names only a kind "
            "of item (\"pasta\", \"pizza\"), don't choose one for them: pass their own words to add_to_cart.")


def _fallback(state: State) -> dict:
    """Never a dead end: in the customer's language, with whatever the tools really found as cards."""
    lang = state.get("language") if state.get("language") in FALLBACKS else "roman_urdu" \
        if state.get("language") == "mixed" else "latin"
    found = _found_cards(state.get("tool_results") or [])
    return {"answer": (FOUND_FALLBACKS if found else FALLBACKS)[lang], "fallback": True}


def _which_one_cards(results: List[dict]) -> List[dict]:
    """The cart asked "which one?" / "which size?": always show those items as cards, whatever the answer says."""
    asked = [r for r in results if any(p.get("error") in ("which_one", "size_required", "unknown_size")
                                       for p in r.get("problems") or [])]
    return _found_cards(asked[-1:]) if asked else []


def _found_cards(results: List[dict]) -> List[dict]:
    """Every item the tools returned this turn, as cards (the answer text names none of them)."""
    cards, seen = [], set()
    for r in results:
        for it in (r.get("items") or []) + (r.get("deals") or []) + ([r["item"]] if r.get("item") else []):
            key = (it.get("name", "").lower(), it.get("price"), it.get("price_from"))
            if not it.get("item_id") or key in seen or it.get("available") is False:
                continue
            seen.add(key)
            cards.append({k: it.get(k) for k in ("item_id", "name", "type", "category", "price", "price_from", "sizes",
                                                 "currency", "available", "image_url")})
    return repo.merge_duplicates(cards)[:5]


def _cards(answer: str, results: List[dict]) -> List[dict]:
    """Item cards for the app UI: items from this turn's tool results that the answer mentions."""
    def words(s: str) -> str:  # spacing/punctuation-insensitive: models write "Amazing\u202f1", "**Euro**"
        return f" {re.sub(r'[^0-9a-z]+', ' ', s.lower()).strip()} "

    said, seen, cards = words(answer), set(), []
    for r in results:
        for it in (r.get("items") or []) + (r.get("deals") or []) + ([r["item"]] if r.get("item") else []):
            key = (it["name"].lower(), it.get("price"), it.get("price_from"))
            if key in seen or words(it["name"]) not in said:
                continue
            seen.add(key)
            cards.append({k: it.get(k) for k in ("item_id", "name", "type", "category", "price", "price_from", "sizes",
                                                 "currency", "available", "image_url")})
    return repo.merge_duplicates(cards)[:5]


def _suggestions(called: List[str]) -> List[str]:
    if "review_order" in called:
        return ["Review & confirm order", "Show my cart"]
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
        # A cart tool ran before the LLM became unavailable: report what really happened, from code.
        if (state.get("cart") or []) != (state.get("cart_before") or []):
            cart = price_cart(state.get("cart") or [], ctx.branch_id)
            return {"answer": f"Done, your cart is updated. Subtotal: Rs {cart['subtotal']} "
                              f"(tax and delivery are added in the app at checkout).",
                    "items": [], "suggestions": ["Show my cart"], "handoff": False, "as_of": utc_now_iso()}
        sizes = [(p.get("item"), p.get("valid_sizes")) for r in state.get("tool_results") or []
                 for p in (r.get("problems") or []) if p.get("error") in ("size_required", "unknown_size")]
        if sizes and sizes[0][1]:
            name, valid = sizes[0]
            options = ", ".join(f"{v['size']} Rs {v['price']}" if isinstance(v, dict) else str(v) for v in valid)
            return {"answer": f"{name or 'This item'} comes in different sizes ({options}). Which size would you like?",
                    "items": [], "suggestions": [], "handoff": False, "as_of": utc_now_iso()}
        return {"answer": "I couldn't update your cart just now. Please try again in a minute.",
                "items": [], "suggestions": ["Show my cart"], "handoff": False, "as_of": utc_now_iso()}
    # Same search the agent uses (merged duplicates, item types, exactness), so the cards are consistent
    # with normal answers; the card buttons add to the cart without the LLM.
    try:
        found = search_menu(replace(ctx, user_message=message), SearchMenuArgs(query=message[:200]))
    except PyMongoError:
        raise  # DB down too: format_response returns the clear error instead
    except Exception:  # noqa: BLE001
        found = {"items": []}
    items = found.get("items") or []
    if not items:
        text = "Our assistant is busy right now. Please try again in a few minutes."
    elif found.get("exact") is False or found.get("match") == "several":
        text = ("Our assistant is busy right now, so I can't answer in full. These menu items are the closest "
                "matches; tap a size to add one to your cart, or ask again in a few minutes.")
    else:
        text = ("Our assistant is busy right now, so I can't answer in full. Here's what I found; tap a size to "
                "add it to your cart, or ask again in a few minutes.")
    return {"answer": text, "items": items[:5], "suggestions": ["Show my cart"], "handoff": False,
            "as_of": utc_now_iso()}


def _trace(record: dict) -> None:
    TRACE_FILE.parent.mkdir(exist_ok=True)
    with TRACE_FILE.open("a") as f:
        f.write(json.dumps(redact_obj(record), ensure_ascii=False, default=str) + "\n")


# ---------- graph ----------

def build_graph(checkpointer=None):
    g = StateGraph(State)
    g.add_node("pre_checks", pre_checks)
    g.add_node("prefetch", prefetch)
    g.add_node("agent", agent)
    g.add_node("tools", tools)
    g.add_node("post_checks", post_checks)
    g.add_node("format_response", format_response)
    g.add_edge(START, "pre_checks")
    g.add_conditional_edges("pre_checks", route_after_pre_checks, ["prefetch", "format_response"])
    g.add_conditional_edges("prefetch", route_after_tools, ["agent", "format_response"])
    g.add_conditional_edges("agent", route_after_agent, ["tools", "post_checks", "format_response"])
    g.add_conditional_edges("tools", route_after_tools, ["agent", "format_response"])
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


def _run_config(thread_id: str, branch_id: str, user_id: Optional[str], payment_method: Optional[str] = None) -> dict:
    return {"configurable": {"thread_id": thread_id, "user_id": user_id, "branch_id": branch_id,
                             "payment_method": payment_method},
            "recursion_limit": 25}


def _turn_input(message: str, cart: Optional[List[dict]]) -> dict:
    turn = {"messages": [HumanMessage(content=message)]}
    if cart is not None:  # None: keep the cart already in this thread (CLI / Streamlit, one thread per cart)
        turn["cart"] = list(cart)
    return turn


def chat(message: str, *, thread_id: str, branch_id: str, user_id: Optional[str] = None,
         cart: Optional[List[dict]] = None, payment_method: Optional[str] = None) -> dict:
    """One customer turn. user_id must come from verified auth, never from the model or the message.
    cart: the branch's current cart lines; the turn's resulting lines come back in response["cart_lines"].
    payment_method: the cart's chosen method; a new choice comes back in response["payment_method_set"]."""
    out = graph().invoke(_turn_input(message, cart),
                         config=_run_config(thread_id, branch_id, user_id, payment_method))
    return out["response"]


def chat_stream(message: str, *, thread_id: str, branch_id: str, user_id: Optional[str] = None,
                cart: Optional[List[dict]] = None, payment_method: Optional[str] = None) -> Iterator[dict]:
    """Like chat(), but yields {"status": "..."} progress events as the turn runs, then {"response": {...}}."""
    final = None
    for mode, chunk in graph().stream(_turn_input(message, cart),
                                      config=_run_config(thread_id, branch_id, user_id, payment_method),
                                      stream_mode=["custom", "values"]):
        if mode == "custom":
            yield chunk
        else:
            final = chunk
    yield {"response": final["response"]}
