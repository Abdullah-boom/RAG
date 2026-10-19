"""LangGraph agent (spec 10):

  START -> pre_checks -> understand -> prefetch -> agent <-> tools
                                          |
                                          +-> post_checks --(guard failed, 1 retry)--> agent
                                                   |
                                                   +-> format_response -> END

Layers: pre_checks = input hygiene + fixed replies the word lists are sure of (hi, thanks, abuse);
understand = one JSON-schema model call that reads the message into a Plan (app/agent/understand.py);
prefetch = the Plan's workflow: code runs the certain tools, or the agent gets only that intent's tools.

Identity: user_id / branch_id are read from config["configurable"] (set by the server / UI), never from
the model. Memory: RedisSaver (or InMemorySaver in dev) keyed by thread_id; each turn's tool traffic is removed at the end so
only question + answer stay in history (spec: no sensitive tool output in long-term memory).
DB failures: a Mongo read that fails twice (any node) ends the turn with one clear error; the turn is dropped
from memory and its cart changes are undone, so the customer never gets an answer built on partial data.
Cart: the caller passes the branch's cart in (app/cart_store.py owns it, not this thread) and saves
response["cart_lines"] back; the copy in this thread's state is only the working copy for one turn.
Order: the caller also passes the order type, payment choice and the orders placed from chat (hidden ids, for
tracking). A ready review_order is answered with app/order_flow.py's fixed summary (response["review_ready"]),
and a delivery / pickup switch comes back as response["order_type_set"]: the caller applies both.
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
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import ValidationError
from pymongo.errors import PyMongoError

from app import config as settings
from app import order_flow, payments
from app.agent import behaviour, intents, understand
from app.agent.intents import ALL_TOPICS, QUESTION, add_call, prefetch_tools, tools_for, topics
from app.cart import chosen_from_offered, pointed_item
from app.agent.guards import (LANGUAGE_HINTS, MAX_MESSAGE_CHARS, NOT_ROMAN_URDU, REPLY_NOTES, URDU_CHAR, check_answer,
                               detect_language, pre_check, redact_obj)
from app.data import repositories as repo
from app.data.db import with_retry
from app.retrieval import index
from app.cart import price_cart
from app.payments import find_card_number, redact_card_numbers
from app.tools import (DB_UNAVAILABLE, RequestContext, SearchMenuArgs, _lenient, run_tool_with_cart, search_menu,
                       tool_schemas, utc_now_iso)

TIME_BUDGET_S = settings.LLM_TIME_BUDGET_S
MAX_RATE_LIMIT_WAIT_S = 8
# When no draft passes the guard (the model's answer couldn't be verified against this turn's lookups). No item
# cards then: cards only ever come with a real answer that gives them their context.
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
WHICH_ONE = {
    "latin": "There's more than one that fits. Which one would you like? Tap one below, or tell me its name.",
    "roman_urdu": "Is mein ek se zyada options hain. Aap kaunsa lena chahenge? Neeche se chunein ya naam batayein.",
    "mixed": "Is mein ek se zyada options hain. Aap kaunsa lena chahenge? Neeche se chunein ya naam batayein.",
    "urdu_script": "اس میں ایک سے زیادہ آپشن ہیں۔ آپ کون سا لینا چاہیں گے؟ نیچے سے چنیں یا نام بتائیں۔",
}
# The cart added nothing and needs one answer from the customer: code asks (the model wrote "Aapka cart ab yeh hai:
# Chicken Tikka" when nothing had been added).
SIZE_ASK = {
    "latin": "{item} comes in {sizes}. Which size would you like?",
    "roman_urdu": "{item} in sizes mein aata hai: {sizes}. Aap kaunsa size lena chahenge?",
    "urdu_script": "{item} ان سائز میں آتا ہے: {sizes}۔ آپ کون سا سائز لینا چاہیں گے؟",
}
ASK_ERRORS = {"size_required", "unknown_size", "which_one"}
DID_YOU_MEAN = {
    "latin": "We don't have exactly that. Did you mean this one? Tap it to add, or tell me what you'd like.",
    "roman_urdu": "Bilkul yehi to nahi hai. Kya aap ka matlab yeh hai? Add karne ke liye tap karein, ya batayein kya "
                  "chahiye.",
    "urdu_script": "بالکل یہی تو نہیں ہے۔ کیا آپ کا مطلب یہ ہے؟ شامل کرنے کے لیے ٹیپ کریں، یا بتائیں کیا چاہیے۔",
}
FALLBACKS = {
    "latin": SAFE_FALLBACK,
    "roman_urdu": "Maaf kijiye, iska pakka jawab nahi bana saka. Thora alag tareeqe se poochein, ya app mein menu "
                  "dekh lein.",
    "urdu_script": "معذرت، اس کا پکا جواب نہیں بن سکا۔ ذرا مختلف طریقے سے پوچھیں، یا ایپ میں مینو دیکھ لیں۔",
}
NOT_ON_MENU = {
    "latin": "Sorry, we don't have that on the menu at this branch. Would you like me to suggest something else?",
    "roman_urdu": "Maaf kijiye, yeh is branch ke menu mein nahi hai. Kuch aur dikhaun?",
    "urdu_script": "معذرت، یہ اس برانچ کے مینو میں نہیں ہے۔ کچھ اور دکھاؤں؟",
}
# The model is unreachable and code can't answer this message itself: say so plainly, with no item cards.
BUSY = {
    "latin": "Sorry, our assistant is busy right now. Please try again in a few minutes.",
    "roman_urdu": "Maaf kijiye, hamara assistant abhi busy hai. Thori der baad dobara try karein.",
    "urdu_script": "معذرت، ہمارا اسسٹنٹ ابھی مصروف ہے۔ کچھ دیر بعد دوبارہ کوشش کریں۔",
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
    "review_order": "Checking your order…", "set_order_type": "Updating your order…",
    "set_quantity": "Updating your cart…",
}
LLM_ONLY_DROP = {"image_url", "currency", "sold_at_this_branch", "is_deal", "as_of"}


class State(TypedDict, total=False):
    messages: Annotated[List[AnyMessage], add_messages]  # persisted conversation
    cart: List[dict]  # this turn's working copy of the branch cart {item_id, name, size, qty}; never submitted
    offered: List[dict]  # items the last answer showed, in order: what "the first one" / "yeh wala" refers to
    pay_offered: List[str]  # payment methods the last answer listed, in order: what "1" / "pehla wala" picks
    # per-turn scratch (reset in pre_checks)
    turn_id: str
    language: str
    started_at: float
    tool_results: List[dict]
    tools_called: List[str]
    steps: List[dict]
    retried: bool
    answer: Optional[str]
    prefetched: bool  # code already ran the lookup this turn: the model only words the reply (no tool schemas)
    topics: List[str]  # rule packs + tools this message needs (intents.topics); all of them when unclear
    fallback: bool  # no draft passed the guard: answer is FALLBACKS, no cards
    offline: bool
    db_error: bool  # a Mongo read failed this turn: answer with one clear error, nothing partial
    cart_before: List[dict]  # restored if the turn fails
    response: Dict[str, Any]
    plan: Optional[dict]  # understand's reading of this message; None = word-list path
    forced: Optional[list]  # calls code already knows this message needs ("haan" to "shall I place the order?")
    nothing_fits: Optional[dict]  # {"say": "wish"|"item"|"unsure", "what": "gol gol"|"sushi"}: the answer opens so


def _ctx(config: RunnableConfig) -> RequestContext:
    c = config.get("configurable", {})
    return RequestContext(user_id=c.get("user_id"), branch_id=c["branch_id"], payment_method=c.get("payment_method"),
                          order=c.get("order"), tracked=tuple(c.get("tracked") or ()))


def _turn_messages(state: State) -> List[AnyMessage]:
    """Messages from this turn's HumanMessage onward."""
    msgs = state["messages"]
    for i, m in enumerate(msgs):
        if m.id == state["turn_id"]:
            return msgs[i:]
    return msgs[-1:]


def _history(state: State) -> List[AnyMessage]:
    """The last HISTORY_TURNS exchanges, newest first kept, within HISTORY_MAX_CHARS."""
    msgs = state["messages"]
    past = next((msgs[:i] for i, m in enumerate(msgs) if m.id == state["turn_id"]), [])
    past = past[-2 * settings.HISTORY_TURNS:]
    kept, size = [], 0
    for m in reversed(past):
        size += len(m.content if isinstance(m.content, str) else str(m.content))
        if size > settings.HISTORY_MAX_CHARS:
            break
        kept.append(m)
    kept.reverse()
    if kept and not isinstance(kept[0], HumanMessage):  # never start mid-exchange
        kept = kept[1:]
    return kept


# ---------- LLM ----------

OLLAMA_NUM_CTX = settings.OLLAMA_NUM_CTX  # the planner must use the same value, or Ollama reloads the model


@lru_cache(maxsize=1)
def _ollama() -> ChatOllama:
    # num_ctx: Ollama's 4k default silently truncates the system prompt + tool schemas (~4.5k tokens).
    # num_predict caps answer length; keep_alive keeps the model loaded between customers.
    return ChatOllama(model=settings.OLLAMA_MODEL, base_url=settings.OLLAMA_BASE_URL,
                      temperature=settings.TEMPERATURE, num_ctx=OLLAMA_NUM_CTX, num_predict=1024, keep_alive="30m",
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


@lru_cache(maxsize=4)
def _openrouter(model: str, timeout: float = 20) -> ChatOpenAI:
    """OpenRouter: an OpenAI-style API in front of many hosted models (gpt-oss-120b among them)."""
    extra = {"reasoning": {"effort": "low"}} if "gpt-oss" in model else {}  # faster; replies are short anyway
    return ChatOpenAI(model=model, api_key=settings.OPENROUTER_API_KEY, base_url=settings.OPENROUTER_BASE_URL,
                      temperature=settings.TEMPERATURE, max_tokens=1024, max_retries=1, timeout=timeout,
                      extra_body=extra or None, default_headers={"X-Title": "Cheezious Assistant"})


def _retry_after(e: RateLimitError) -> float:
    try:
        return float(e.response.headers.get("retry-after", 60))
    except (TypeError, ValueError, AttributeError):
        return 60.0


TEXT_TOOL_CALL = re.compile(r"^\s*(?:```(?:json)?\s*)?(\{.*\})\s*(?:```)?\s*$", re.S)


def _text_tool_call(content: Any, names: set) -> Optional[dict]:
    """Llama 3.1 via Ollama sometimes writes the tool call as its reply instead of making it:
    {"name": "search_menu", "parameters": {...}}, often with Python False/None. Read it as the call it meant."""
    m = TEXT_TOOL_CALL.match(content) if isinstance(content, str) and names else None
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        try:
            data = json.loads(re.sub(r"\bFalse\b", "false", re.sub(r"\bTrue\b", "true",
                                                                       re.sub(r"\bNone\b", "null", m.group(1)))))
        except json.JSONDecodeError:
            return None
    args = data.get("parameters", data.get("arguments")) if isinstance(data, dict) else None
    if not isinstance(args, dict) or data.get("name") not in names:
        return None
    return {"name": data["name"], "args": args}


def _invoke_llm(messages: List[AnyMessage], allow_tools) -> AIMessage:
    """allow_tools: False (no tools), True (all) or a list of tool names (only those). Ollama: the local model.
    Groq: main model, then fallbacks when rate limited (each Groq model has its own free-tier quota); if all are
    limited but one frees up within a few seconds, wait once and retry."""
    schemas = (tool_schemas(None if allow_tools is True else list(allow_tools))) if allow_tools else None
    if settings.LLM_PROVIDER == "ollama":
        llm = _ollama().bind_tools(schemas) if schemas else _ollama()
        msg = llm.invoke(messages)
        call = _text_tool_call(msg.content, {s["function"]["name"] for s in schemas or []})
        if call and not msg.tool_calls:
            return AIMessage(content="", tool_calls=[{**call, "id": "text_call_0", "type": "tool_call"}])
        return msg
    if settings.LLM_PROVIDER == "openrouter":
        llm = _openrouter(settings.OPENROUTER_MODEL)
        return (llm.bind_tools(schemas) if schemas else llm).invoke(messages)

    def attempt():
        last = None
        for model in [settings.GROQ_MODEL] + settings.GROQ_FALLBACK_MODELS:
            llm = _llm(model).bind_tools(schemas) if schemas else _llm(model)
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


PROMPTS = settings.ROOT / "app" / "agent" / "prompts"
BRANCH_INFO_TTL_S = 60
_branch_info: Dict[str, tuple] = {}


@lru_cache(maxsize=None)
def _prompt_file(name: str) -> str:
    """Prompt parts are read once per process, not on every model call."""
    return (PROMPTS / name).read_text().strip()


def _branch_brief(branch_id: str) -> dict:
    """Branch name and city for the context block, cached briefly (it's in every prompt; it rarely changes)."""
    hit = _branch_info.get(branch_id)
    if hit and time.time() - hit[0] < BRANCH_INFO_TTL_S:
        return hit[1]
    info = repo.get_branch_status(branch_id) or {}
    _branch_info[branch_id] = (time.time(), info)
    return info


CHAT_ONLY = ["chat"]  # greeting-like small talk: greet back and ask what they'd like (code's words, no model)
FIXED_KINDS = {"chat": behaviour.SMALL_TALK, "complain": behaviour.COMPLAIN, "affirm": behaviour.AFFIRM,
               "deny": behaviour.DENY}


def build_system_prompt(ctx: RequestContext, topic_list: List[str], language: str, offered: List[dict]) -> str:
    """core (never changes) + this message's rule packs (fixed order) + the context block last. Stable text first,
    so a provider or Ollama can reuse the start of the prompt across calls."""
    branch = with_retry(_branch_brief, ctx.branch_id)
    shown = ""
    if offered:
        shown = ("Items your last answer showed, in order: "
                 + "; ".join(f"{n}. {o['name']}" + (f" ({o['type']})" if o.get("type") else "")
                             for n, o in enumerate(offered, 1))
                 + ". \"The first one\" / \"pehli wali\" / a size refers to these.\n")
    order_type = (ctx.order or {}).get("type")
    payment = payments.METHODS.get(ctx.payment_method or "", {}).get("name")
    context = _prompt_file("packs/context.md").format(
        branch_name=branch.get("branch", "unknown"), branch_city=branch.get("city", ""),
        order_type=payments.ORDER_TYPE_NAMES.get(order_type, "not chosen yet"),
        payment=payment or "not chosen yet",
        logged_in="yes" if ctx.user_id else "no (guest; order lookups unavailable)",
        now=datetime.now(repo.PK_TZ).strftime("%A %d %B %Y, %I:%M %p"),
        language=LANGUAGE_HINTS[language], offered=shown)
    return "\n\n".join([_prompt_file("core.md")] + [_prompt_file(f"packs/{t}.md") for t in topic_list] + [context])


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
            "steps": [], "retried": False, "answer": None, "fallback": False, "prefetched": False, "topics": [],
            "offline": False, "db_error": False, "plan": None, "nothing_fits": None,
            "cart_before": list(state.get("cart") or []), "forced": None}
    if cleaned is None:
        return {**base, "answer": f"Please send a message under {MAX_MESSAGE_CHARS} characters."}
    if find_card_number(cleaned):
        # Never let a card number reach the model, memory or logs; online payment is the app's checkout.
        language = detect_language(cleaned)
        return {**base, "language": language, "answer": CARD_WARNINGS.get(language, CARD_WARNINGS["latin"]),
                "messages": [HumanMessage(content=redact_card_numbers(cleaned), id=human.id)]}
    language = detect_language(cleaned)
    if not re.search(r"[^\W\d_]", cleaned) and state.get("language"):
        language = state["language"]  # "1", "2" after a list: no words to read, keep the conversation's language
    kind = behaviour.fast_kind(cleaned)
    if kind:  # greeting / thanks / insult / who-are-you: a fixed reply, no tools, nothing for the model to invent
        return {**base, "language": language, "answer": behaviour.reply(kind, language, cleaned),
                "steps": [{"behaviour": kind}], "messages": [HumanMessage(content=cleaned, id=human.id)]}
    return {**base, "language": language,
            "messages": [HumanMessage(content=cleaned, id=human.id)]}  # same id -> replaced in place


def _ask_router(prompt: str) -> str:
    """One short call with no tools and no history: the behaviour layer's FOOD / OTHER question."""
    return _invoke_llm([HumanMessage(prompt)], allow_tools=False).content


PLAN_TIMEOUT_S = 15


@lru_cache(maxsize=1)
def _ollama_planner() -> ChatOllama:
    # format = the Plan's JSON schema: Ollama constrains decoding to it, so the output always parses.
    # num_ctx must equal _ollama()'s: Ollama reloads the model (~7 s on a T4) whenever the context size changes.
    return ChatOllama(model=settings.OLLAMA_MODEL, base_url=settings.OLLAMA_BASE_URL, temperature=0,
                      num_ctx=OLLAMA_NUM_CTX, num_predict=200, keep_alive="30m", format=understand.schema(),
                      client_kwargs={**_ollama_client_kwargs(), "timeout": PLAN_TIMEOUT_S})


def _plan_message(prompt: str) -> Optional[understand.Plan]:
    """L2: one short call, no tools, no history. None when it fails: the word-list path takes over."""
    try:
        if settings.PLANNER_PROVIDER == "ollama":
            text = _ollama_planner().invoke([HumanMessage(prompt)]).content
        elif settings.PLANNER_PROVIDER == "openrouter":
            text = _openrouter(settings.OPENROUTER_MODEL, PLAN_TIMEOUT_S).bind(
                response_format={"type": "json_object"}).invoke([HumanMessage(prompt)]).content
        else:
            text = _llm(settings.GROQ_MODEL).bind(response_format={"type": "json_object"}).invoke(
                [HumanMessage(prompt)]).content
    except Exception:  # noqa: BLE001 - planner down / slow: the word-list path still answers
        return None
    return understand.parse(text)


def _last_reply(state: State) -> str:
    past = next((state["messages"][:i] for i, m in enumerate(state["messages"]) if m.id == state["turn_id"]), [])
    return next((m.content for m in reversed(past) if isinstance(m, AIMessage) and isinstance(m.content, str)), "")


def _recent(state: State, exchanges: int = 2) -> tuple:
    """The last exchanges before this message, as (who, text): the planner's view of the conversation."""
    past = next((state["messages"][:i] for i, m in enumerate(state["messages"]) if m.id == state["turn_id"]), [])
    out = [("customer" if isinstance(m, HumanMessage) else "bot", m.content) for m in past
           if isinstance(m, (HumanMessage, AIMessage)) and isinstance(m.content, str) and m.content]
    return tuple(out[-2 * exchanges:])


def understand_node(state: State) -> dict:
    """L2: what does the customer want? Off-topic gets its fixed reply here; everything else goes to prefetch,
    which picks the workflow from the plan. Routing fails open: a message with a business cue is never refused."""
    said = _turn_messages(state)[0].content
    if _picked_payment(said, state.get("pay_offered") or []):
        # "1" / "pehla wala" right after the payment list: prefetch picks it from code. No model call, which
        # could read a bare number as off-topic and refuse it.
        return {"plan": None, "steps": state["steps"] + [{"plan": "payment_pick"}]}
    if intents.certain(said):  # "place my order", "where is my order?", "pickup kar do": no planner round-trip
        return {"plan": None, "steps": state["steps"] + [{"plan": "word_list"}]}
    answer = order_flow.answer_to_confirm(said)
    if answer:  # a bare yes / no (an order summary's yes never reaches here: the API answers it first)
        if answer == "yes" and any(q in _last_reply(state) for q in order_flow.PLACE_QUESTIONS):
            # "Kuch aur chahiye, ya order place kar doon?" -> "haan": show the order to confirm
            return {"plan": None, "forced": [["review_order", {}]], "steps": state["steps"] + [{"plan": "affirm"}]}
        kind = behaviour.AFFIRM if answer == "yes" else behaviour.DENY
        language = order_flow.answer_language(said, state["language"])  # "haan ji" is too short to detect
        return {"answer": behaviour.reply(kind, language, said), "language": language,
                "steps": state["steps"] + [{"behaviour": kind}]}
    _status("Thinking…")
    plan = _plan_message(understand.prompt(said, _last_reply(state), tuple(state.get("offered") or ()),
                                           tuple(state.get("cart") or ()), _recent(state)))
    # The detected language stands: the model labels plain English ("how much is chicken tiqqa") as Roman Urdu.
    language = state["language"]
    step = {"plan": plan.model_dump(exclude_none=True) if plan else None}
    if plan is None:  # the old path: word lists, then the FOOD / OTHER call for messages with no business cue
        kind = behaviour.kind(said, in_conversation=len(state["messages"]) > 1, ask_model=_ask_router)
        off_topic = kind == behaviour.OFF_TOPIC
    else:
        off_topic = plan.intent == "off_topic" and not behaviour.business(said)
    if off_topic:
        return {"language": language, "answer": behaviour.reply(behaviour.OFF_TOPIC, language, said),
                "steps": state["steps"] + [step, {"behaviour": behaviour.OFF_TOPIC}]}
    return {"language": language, "plan": plan.model_dump() if plan else None, "steps": state["steps"] + [step]}


def agent(state: State, config: RunnableConfig) -> dict:
    ctx = _ctx(config)  # "Thinking…" was already shown by understand_node, which runs before every agent turn
    topic_list = state.get("topics") or list(ALL_TOPICS)
    try:
        system = build_system_prompt(ctx, topic_list, state["language"], state.get("offered") or [])
    except PyMongoError:
        return {"db_error": True, "steps": state["steps"] + [{"error": DB_UNAVAILABLE}]}
    turn = _turn_messages(state)
    note = REPLY_NOTES.get(state["language"])
    if note:  # prompt copy only: memory and the guards keep the customer's own words
        turn = [HumanMessage(content=f"{turn[0].content}\n\n{note}")] + turn[1:]
    prompt = [SystemMessage(system)] + _history(state) + turn  # one system message, then the conversation
    try:
        # When code already ran the lookup, the model only words the reply: no second add, and no tool schemas
        # (~2k tokens per call; Groq's free tier allows ~8k tokens a minute).
        allow = (tools_for(topic_list) if len(state["tools_called"]) < settings.MAX_TOOL_CALLS
                 and not state.get("prefetched") else False)
        msg = _invoke_llm(prompt, allow_tools=allow)
    except Exception as e:  # noqa: BLE001 - LLM down / timeout / quota: fall back, never crash
        return {"offline": True, "steps": state["steps"] + [{"error": type(e).__name__}]}
    # The real prompt size, to check it fits OLLAMA_NUM_CTX (Ollama reports it; Groq reports usage instead).
    meta = msg.response_metadata or {}
    used = meta.get("prompt_eval_count") or (meta.get("token_usage") or {}).get("prompt_tokens")
    return {"messages": [msg], "steps": state["steps"] + [{"prompt_tokens": used}]} if used else {"messages": [msg]}


def _customer_words(state: State) -> str:
    """The customer's message, as the cart and search compare it with English menu names. In Urdu script the
    letters can't match ("2 لارج کراؤن کرسٹ"), so the planner's English reading of the items is added."""
    said = _turn_messages(state)[0].content
    plan = state.get("plan") or {}
    if URDU_CHAR.search(said) and plan:
        said += " " + " ".join([i.get("name", "") + " " + (i.get("size") or "") for i in plan.get("items") or []]
                               + [plan.get("wish") or ""])
    return said


def tools(state: State, config: RunnableConfig) -> dict:
    ctx = replace(_ctx(config), user_message=_customer_words(state), offered=tuple(state.get("offered") or ()))
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
    picked = _picked_payment(said, state.get("pay_offered") or [])
    if picked:  # "2" / "pehla wala" right after the payment list: that method, from code
        return _run_prefetch(state, config, [("set_payment_method", {"method": picked})],
                             ["menu", "cart", "payment", "checkout"], None)
    plan = understand.Plan.model_validate(state["plan"]) if state.get("plan") else None
    if state.get("forced"):
        return _run_prefetch(state, config, [tuple(c) for c in state["forced"]], ["menu", "cart", "payment", "checkout"],
                             None)
    if plan:
        calls, topic_list = understand.route(plan, said, offered, tuple(state.get("cart") or ()))
        if topic_list and topic_list[0] in understand.FIXED_REPLY:
            # Small talk, a complaint, a bare yes / no: code's reply (the 8B model invented "teen burgers" for
            # "aur sunao" and promised a refund for a complaint).
            kind = FIXED_KINDS[topic_list[0]]
            return {"topics": topic_list, "answer": behaviour.reply(kind, state["language"], said),
                    "steps": state["steps"] + [{"behaviour": kind}]}
        if not calls:
            return {"topics": topic_list}
        return _run_prefetch(state, config, calls, topic_list, plan)
    calls = prefetch_tools(said)
    if not calls and offered:
        pointed = pointed_item(said, offered)
        if pointed and QUESTION.search(said):  # "pehli wali kitne ki hai?": look up the item pointed at
            calls = [("search_menu", {"query": pointed["name"], "picked": True})]
        elif not QUESTION.search(said) and chosen_from_offered(said, offered):
            calls = [add_call(said)]  # replying to "which one?" with a name: that's choosing it
    topic_list = topics(said, calls, bool(offered))
    if not calls:
        return {"topics": topic_list}
    return _run_prefetch(state, config, calls, topic_list, None)


NOTHING_FITS = ("Nothing on our menu matches \"{wish}\"; the reply already starts by saying so. Just present a few "
                "of these best sellers as other options (name, type and price exactly as given).")
# Said by code, not left to the model (it skipped the sentence and offered Alfredo pasta as "something sweet").
NOTHING_FITS_SAY = {
    "latin": "We don't have anything {wish} on the menu right now, but these are our best sellers:",
    "roman_urdu": "Abhi menu mein \"{wish}\" jaisa kuch nahi hai, lekin yeh hamare best sellers hain:",
    "urdu_script": "ابھی مینو میں \"{wish}\" جیسا کچھ نہیں ہے، لیکن یہ ہمارے بیسٹ سیلرز ہیں:",
}
# Asked for a lower price: prices are fixed, said by code; the deals are the real way to save.
NEGOTIATE_NOTE = ("The customer asked for a discount; the reply already says prices are fixed. Just present a few of "
                  "these deals and any active discounts (names and prices exactly as given). Never offer a discount, "
                  "refund or free item.")
NEGOTIATE_SAY = {
    "latin": "Sorry, our prices are fixed, so I can't give a discount myself. These deals are the best way to save:",
    "roman_urdu": "Maaf kijiye, hamari prices fixed hain, is liye main khud discount nahi de sakta. Bachat ke liye "
                  "yeh deals behtareen hain:",
    "urdu_script": "معذرت، ہماری قیمتیں مقرر ہیں، اس لیے میں خود رعایت نہیں دے سکتا۔ بچت کے لیے یہ ڈیلز بہترین ہیں:",
}
def _negotiate_answer(results: List[dict], lang: str) -> str:
    deals = sorted((d for r in results for d in r.get("deals") or [] if d.get("price") or d.get("price_from")),
                   key=lambda d: d.get("price") or d.get("price_from"))[:5]
    rows = "\n".join(f"{n}. {d['name']} – Rs {(d.get('price') or d.get('price_from')):,}" for n, d in enumerate(deals, 1))
    return NEGOTIATE_SAY[lang] + ("\n\n" + rows if rows else "")


# The planner doesn't know what the customer's words mean ("dhinchak"): ask, and offer best sellers as a lead.
UNSURE_NOTE = ("We're not sure what the customer means by \"{wish}\"; the reply already starts by asking. Just present "
               "a few of these best sellers as ideas (name, type and price exactly as given).")
UNSURE_SAY = {
    "latin": "I'm not sure what you mean by \"{wish}\". Could you tell me a bit more, like a taste, an item or a "
             "budget? Meanwhile, here are our best sellers:",
    "roman_urdu": "\"{wish}\" se aap ka kya matlab hai? Thora aur batayein, jaise taste, koi item ya budget. Tab tak "
                  "yeh hamare best sellers hain:",
    "urdu_script": "\"{wish}\" سے آپ کا کیا مطلب ہے؟ تھوڑا اور بتائیں، جیسے ذائقہ، کوئی آئٹم یا بجٹ۔ تب تک یہ ہمارے "
                   "بیسٹ سیلرز ہیں:",
}
NOT_ON_MENU_NOTE = ("We don't have \"{item}\": the reply already starts by saying so. These are only the closest "
                    "items: offer them as alternatives, never as {item}.")
NOT_ON_MENU_SAY = {
    "latin": "Sorry, we don't have {item} on the menu. You might like:",
    "roman_urdu": "Maaf kijiye, menu mein {item} nahi hai. Yeh try kar sakte hain:",
    "urdu_script": "معذرت، مینو میں {item} نہیں ہے۔ یہ آزما سکتے ہیں:",
}


def _run_prefetch(state: State, config: RunnableConfig, calls, topic_list: List[str],
                  plan: Optional[understand.Plan]) -> dict:
    """Run the calls as if the model had made them; the model then only words the reply."""
    call = AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": f"prefetch_{i}", "type": "tool_call"}
                                             for i, (n, a) in enumerate(calls)])
    out = tools({**state, "messages": state["messages"] + [call]}, config)
    out["messages"] = [call] + out.get("messages", [])
    # The customer's own words go in the reply ("gol gol"), never the model's reading of them.
    words = (plan.wish_said or plan.wish) if plan else None
    if plan and plan.intent == "recommend" and words and plan.unsure() and not out.get("db_error"):
        msgs = list(out["messages"])
        for n, m in enumerate(msgs):
            if isinstance(m, ToolMessage) and m.name == "get_best_sellers":
                msgs[n] = ToolMessage(content=_for_llm({**json.loads(m.content), "note": UNSURE_NOTE.format(
                    wish=words)}), tool_call_id=m.tool_call_id, name=m.name)
        out = {**out, "messages": msgs, "nothing_fits": {"say": "unsure", "what": words}}
    elif plan and plan.intent == "recommend" and (plan.wish or plan.avoid) and not out.get("db_error"):
        out = _ground_wish(out, plan.wish, plan.avoid)
    if plan and plan.intent == "recommend" and (plan.wish or plan.avoid) and not plan.unsure() \
            and not out.get("db_error") \
            and not any(r.get("items") or r.get("deals") for r in out.get("tool_results") or []):
        # A craving the menu has nothing for ("kuch khatta"): best sellers, so the reply has real items to offer
        # instead of an empty search the model fills with invented dishes.
        more = AIMessage(content="", tool_calls=[{"name": "get_best_sellers", "args": {}, "id": "prefetch_alt",
                                                  "type": "tool_call"}])
        extra = tools({**state, **out, "messages": state["messages"] + out["messages"] + [more]}, config)
        if extra.get("db_error"):
            return {**out, **extra, "messages": out["messages"]}
        msgs = extra.get("messages") or []
        if msgs:
            result = json.loads(msgs[-1].content)
            msgs[-1] = ToolMessage(content=_for_llm({**result, "note": NOTHING_FITS.format(wish=words)}),
                                   tool_call_id="prefetch_alt", name="get_best_sellers")
        out = {**out, **{k: v for k, v in extra.items() if k != "messages"},
               "messages": out["messages"] + [more] + msgs, "nothing_fits": {"say": "wish", "what": words}}
    if plan and plan.intent == "negotiate" and not out.get("db_error"):
        # Code's words, deals included: the model kept quoting the customer's earlier budget ("Rs 3000") and failed
        # the price guard.
        out["answer"] = _negotiate_answer(out.get("tool_results") or [], _reply_lang(state))
    if plan and plan.intent == "item_info" and not out.get("db_error"):
        out = _mark_missing_items(out)
    fixed = None if out.get("db_error") else _fixed_answer(out.get("tool_results") or [], _reply_lang(state))
    if fixed:  # order summary / payment choice / delivery-pickup switch: fixed text, no model call needed
        out["answer"] = fixed
    out["prefetched"] = True
    out["topics"] = topic_list
    return out


def _ground_wish(out: dict, wish: Optional[str], avoid: Optional[str] = None) -> dict:
    """Keep only search results that really fit the description (understand.grounded), in both copies: the tool
    results (guard, cards) and the trimmed messages the model reads. A whole kind of item ("burger") is already
    exact. No item left = the menu has nothing like it: the caller says so."""
    def ground(result: dict) -> dict:
        if result.get("match") == "category" or not result.get("items"):
            return result
        kept = understand.grounded(wish, result["items"], avoid)
        return {**result, "items": kept, **({} if kept else {"note": "no_match"})}

    results = [ground(r) for r in out.get("tool_results") or []]  # recommend prefetch ran only the search
    msgs = [ToolMessage(content=_for_llm(ground(json.loads(m.content))), tool_call_id=m.tool_call_id, name=m.name)
            if isinstance(m, ToolMessage) and m.name in ("search_menu", "get_best_sellers") else m
            for m in out["messages"]]
    return {**out, "messages": msgs, "tool_results": results}


MISSING_WORDS = re.compile(r"don['’]?t have|do not have|not on (our|the) menu|couldn['’]?t find|could not find"
                           r"|nahi hai|nahin hai|nahi mila|نہیں ہے|نہیں مل", re.I)


def _says_missing(answer: str, what: str) -> bool:
    """The model already said we don't have it: code doesn't say it a second time."""
    first = re.split(r"(?<=[.!?\n۔؟])\s", answer.strip(), maxsplit=1)[0]  # Urdu full stop / question mark too
    return bool(MISSING_WORDS.search(first)) and any(w in first.lower() for w in what.lower().split())


def _mark_missing_items(out: dict) -> dict:
    """A named item we don't have ("sushi"): the search still returns its nearest neighbours (burgers). Say it's
    not on the menu from code, and tell the model the items are alternatives."""
    msgs = list(out["messages"])
    for n, m in enumerate(msgs):
        if not (isinstance(m, ToolMessage) and m.name == "search_menu"):
            continue
        asked = next((c["args"].get("query", "") for c in msgs[0].tool_calls if c["id"] == m.tool_call_id), "")
        result = json.loads(m.content)
        if result.get("exact") is False or not understand.missing_item(asked, result.get("items") or [],
                                                                       index().corrections(asked)):
            continue
        msgs[n] = ToolMessage(content=_for_llm({**result, "note": NOT_ON_MENU_NOTE.format(item=asked)}),
                              tool_call_id=m.tool_call_id, name=m.name)
        out = {**out, "nothing_fits": {"say": "item", "what": asked}}
    return {**out, "messages": msgs}


def post_checks(state: State, config: RunnableConfig) -> dict:
    """Price + grounding guard. One retry with feedback, then a safe fallback."""
    draft = (state["messages"][-1].content or "").strip()
    if not draft:
        # No text to check: the time budget ran out while the model still wanted a tool. Answer safely
        # instead of looping back to the agent (an empty answer used to route there until the recursion limit).
        return {**_fallback(state), "steps": state["steps"] + [{"draft": "", "guard_problems": ["no_answer_in_time"]}]}
    turn = _turn_messages(state)
    # The customer's branch (cached for the prompt already) may be named; no extra database call on this path.
    here = (_branch_info.get(_ctx(config).branch_id) or (0, {}))[1].get("branch")
    problems = check_answer(draft, state["tool_results"], state["tools_called"], turn[0].content, _menu_names(),
                            [here] if here else [])
    steps = state["steps"] + [{"draft": draft, "guard_problems": problems}]
    if not problems:
        return {"answer": draft, "steps": steps}
    if _nothing_on_menu(steps):
        # The menu has none: say so plainly instead of letting the model try (and invent items) again.
        return {"answer": NOT_ON_MENU[_reply_lang(state)], "steps": steps}
    if state["retried"] or time.time() - state["started_at"] > TIME_BUDGET_S:
        # A draft whose facts passed and whose only fault was English instead of Roman Urdu beats the
        # fallback. Not an Urdu-script slip: small models garble it ("yہ رہی آپکی کارت").
        usable = [s["draft"] for s in steps if s.get("draft") and s.get("guard_problems") == [NOT_ROMAN_URDU]
                  and not s["draft"].lstrip().startswith("{")]  # never a raw tool call the model wrote as text
        return {"answer": usable[-1], "steps": steps} if usable else {**_fallback(state), "steps": steps}
    _status("Double-checking the answer…")
    # After prefetch the model has no tools: asking it to "call the right tool" made it write a tool call as text.
    fix = ("Write the answer again using only facts from the tool results above; don't call tools."
           if state.get("prefetched") else "Call the right tool if needed and answer again using only facts from "
                                           "tool results.")
    feedback = HumanMessage("[system check] Your reply failed verification: " + "; ".join(problems) + ". " + fix)
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
                fixed = _fixed_answer(state.get("tool_results") or [], _reply_lang(state))
                if fixed:  # the order summary, a payment choice, a delivery / pickup switch: always code's words
                    answer = fixed
                fits = None if fixed else state.get("nothing_fits")
                if fits and not state.get("fallback") and not _says_missing(answer, fits["what"]):
                    say = {"wish": NOTHING_FITS_SAY, "unsure": UNSURE_SAY,
                           "negotiate": NEGOTIATE_SAY}.get(fits["say"], NOT_ON_MENU_SAY)
                    answer = say[_reply_lang(state)].format(wish=fits["what"], item=fits["what"]) + "\n\n" + answer
                cards = ([] if state.get("fallback")  # no real answer: no cards without context
                         else _which_one_cards(state["tool_results"]) or _cards(answer, state["tool_results"]))
                response = {"answer": answer, "items": cards,
                            "suggestions": _suggestions(state["tools_called"], state.get("topics")), "handoff": False,
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
    _trace({"at": response["as_of"], "model": _model_name(), "user_id": ctx.user_id, "branch_id": ctx.branch_id,
            "message": question,
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
    switched = [r for r in state.get("tool_results") or [] if r.get("order_type_selected")]
    if switched:
        response["order_type_set"] = {k: switched[-1].get(k) for k in ("order_type_selected", "needs")}
    response["language"] = _reply_lang(state)  # for the caller's own fixed replies (the Confirm button's answer)
    if _ready_review(state.get("tool_results") or []):
        response["review_ready"] = True  # the caller keeps a confirm token: the customer's "yes" places it
    # Keep only question + final answer in memory; drop tool calls/results and guard feedback.
    removals = [RemoveMessage(id=m.id) for m in turn[1:]]
    # What "the first one" / "large" / "Crown Crust" refers to next: this answer's cards; with no new cards the
    # last list stays, until the cart changes (then an old list would point at the wrong thing).
    offered = [{"item_id": i["item_id"], "name": i["name"], "type": i.get("type")} for i in response["items"]]
    before = state.get("offered") or []
    unchanged_cart = (state.get("cart") or []) == (state.get("cart_before") or [])
    names = lambda items: {i["name"].strip().lower() for i in items}  # by name: the menu repeats items under new ids
    if unchanged_cart and (not offered or names(offered) <= names(before)):
        # Nothing new shown, or only a look at one of the listed items ("pehli wali kitne ki hai?"): the list
        # stays, so "dusri wali" next still means the second item of that list.
        offered = before
    return {"response": response, "messages": removals + [AIMessage(content=response["answer"])], "offered": offered,
            "pay_offered": _listed_payments(state.get("tool_results") or [])}


# ---------- routing ----------

def route_after_pre_checks(state: State) -> str:
    return "format_response" if state.get("answer") else "understand"


def route_after_understand(state: State) -> str:
    return "format_response" if state.get("answer") else "prefetch"


def route_after_agent(state: State) -> str:
    if state.get("offline") or state.get("db_error"):
        return "format_response"
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None) and time.time() - state["started_at"] < TIME_BUDGET_S:
        return "tools"
    return "post_checks"


def route_after_tools(state: State) -> str:
    return "format_response" if state.get("db_error") or state.get("answer") else "agent"


def route_after_post_checks(state: State) -> str:
    return "format_response" if state.get("answer") is not None else "agent"


# ---------- helpers ----------

def _reply_lang(state: State) -> str:
    return state.get("language") if state.get("language") in FALLBACKS else "roman_urdu" \
        if state.get("language") == "mixed" else "latin"


def _fallback(state: State) -> dict:
    """No draft could be verified: say so in the customer's language and ask them to rephrase. No cards: items
    without an answer that explains them read as the bot's pick (a Pizza Stacker for "chicken tikka pizza")."""
    return {"answer": FALLBACKS[_reply_lang(state)], "fallback": True}


def _nothing_on_menu(steps: List[dict]) -> bool:
    """Every tool call this turn was a plain menu search (no budget, not deals-only) that matched nothing.
    A filtered search finding nothing means "nothing under that budget", not "we don't have it"."""
    calls = [s for s in steps if "tool" in s]
    if not calls:
        return False
    for s in calls:
        if s["tool"] != "search_menu" or (s.get("result") or {}).get("note") != "no_match":
            return False
        try:
            args = SearchMenuArgs.model_validate(_lenient(SearchMenuArgs, s.get("args") or {}))
        except ValidationError:
            return False
        if args.max_price is not None or args.deals_only:
            return False
    return True


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
    cards.sort(key=lambda c: said.find(words(c["name"])))  # in the order the answer lists them
    return repo.merge_duplicates(cards)[:5]


def _review_of(r: dict) -> Optional[dict]:
    """A review in a tool result: review_order's own, or the one set_payment_method runs once a method is set."""
    return r if "ready" in r and "problems" in r else r.get("review")


def _ready_review(results: List[dict]) -> Optional[dict]:
    """The turn's last review, if the order is ready to place."""
    reviews = [rv for rv in map(_review_of, results) if rv]
    return reviews[-1] if reviews and reviews[-1]["ready"] else None


ORDINALS = {"first": 1, "pehla": 1, "pehli": 1, "pehle": 1, "second": 2, "dusra": 2, "dusri": 2, "doosra": 2,
            "doosri": 2, "third": 3, "teesra": 3, "teesri": 3, "tisra": 3, "fourth": 4, "chautha": 4, "chothi": 4,
            "fifth": 5, "panchwa": 5, "last": -1, "akhri": -1, "aakhri": -1}
PICK = re.compile(r"^\s*(?:option|number|no\.?|#)?\s*(\d{1,2}|[a-z]+)(?:st|nd|rd|th)?\s*(?:one|wala|wali|walay|option"
                  r"|number|please|plz|pls|ji)?\s*[.!]?\s*$", re.I)


def _picked_payment(said: str, listed: List[str]) -> Optional[str]:
    """The method the customer picked by number or position from the list the last answer showed, or None."""
    m = PICK.match(said or "") if listed else None
    if not m:
        return None
    word = m.group(1).lower()
    n = int(word) if word.isdigit() else ORDINALS.get(word)
    if n is None or not (n == -1 or 1 <= n <= len(listed)):
        return None
    return listed[n - 1] if n > 0 else listed[-1]


def _listed_payments(results: List[dict]) -> List[str]:
    """Payment methods this turn's answer listed (numbered, in this order), so the next "2" picks one."""
    for r in reversed(results):
        rv = _review_of(r) or {}
        methods = (r.get("methods") if "cart_subtotal" in r and "payment_method_selected" not in r else None) \
            or rv.get("payment_options") or (r.get("available") if r.get("error") == "not_for_this_order_type" else None)
        if methods and not r.get("payment_method_selected"):
            return [m["name"] for m in methods]
    return []


def _fixed_answer(results: List[dict], lang: str) -> Optional[str]:
    """Code's own reply when the turn is about the order itself (the model invented totals, branches and empty
    carts here): a ready review_order is always answered with the summary; a review that isn't ready, a payment
    list or choice, or a delivery / pickup switch when that is all the turn did. None: the model words it (and
    the guard checks it)."""
    reviews = [r for r in results if "ready" in r and "problems" in r]
    if reviews and reviews[-1]["ready"]:
        return order_flow.summary_text(reviews[-1], lang)
    texts = [order_flow.not_ready_text(r, lang) if r in reviews
             else order_flow.cart_text(r, lang, _cart_question(r, lang)) if "cart" in r and "added" in r
             else order_flow.cart_text(r, lang) if "cart" in r and ("removed" in r or "updated" in r
                                                                    or r.get("cleared") or set(r) <= {"cart", "as_of"}
                                                                    or r.get("error") == "not_in_cart")
             else order_flow.action_text(r, lang) for r in results]
    return "\n\n".join(texts) if texts and all(texts) else None


def _cart_question(result: dict, lang: str) -> Optional[str]:
    """What an add still needs from the customer: which size, or which of several items (their cards show under
    it). Asked after whatever was added. None when something else went wrong (the model words that)."""
    problems = result.get("problems") or []
    if "added" not in result or not problems or any(p.get("error") not in ASK_ERRORS for p in problems):
        return None
    asks = []
    for p in problems:
        if p["error"] == "which_one":
            one = len(p.get("options") or []) == 1  # "pepsi" -> only Soft Drink fits: ask about that one
            asks.append((DID_YOU_MEAN if one else WHICH_ONE).get(lang, WHICH_ONE["latin"]))
            continue
        valid = p.get("valid_sizes") or []
        sizes = ", ".join(f"{v['size']} Rs {v['price']:,}" if isinstance(v, dict) and v.get("price") is not None
                          else str(v.get("size") if isinstance(v, dict) else v) for v in valid)
        if not sizes:
            return None
        asks.append(SIZE_ASK.get(lang, SIZE_ASK["latin"]).format(item=p.get("item") or "This item", sizes=sizes))
    return "\n\n".join(dict.fromkeys(asks))


def _suggestions(called: List[str], topic_list: Optional[List[str]] = None) -> List[str]:
    if "review_order" in called:
        return ["Show my cart"]
    if "set_order_type" in called:
        return ["Payment options", "Show my cart"]
    if any(c in called for c in ("add_to_cart", "remove_from_cart", "view_cart")):
        return ["Show my cart", "Add a drink", "Show deals"]
    if "get_order_status" in called or "get_order_history" in called:
        return ["Show my past orders", "Show deals"]
    if "get_deals" in called:
        return ["Is my branch open?", "Something spicy"]
    if topic_list == CHAT_ONLY:  # small talk: offer the easy ways in
        return ["Show deals", "Best sellers", "Something spicy"]
    return ["Show deals", "Is my branch open?"]


CART_TOOLS = {"add_to_cart", "remove_from_cart", "clear_cart"}


def _offline_response(ctx: RequestContext, message: str, state: State) -> dict:
    """LLM unavailable. What code already did this turn is reported from code (a cart change, or the cart's own
    "which size?" with that item's card); anything else gets an honest "busy, try again" with no cards, never search
    results the customer would read as the bot's pick."""
    if CART_TOOLS & set(state.get("tools_called") or []):
        # A cart tool ran before the LLM became unavailable: report what really happened, from code.
        if (state.get("cart") or []) != (state.get("cart_before") or []):
            cart = price_cart(state.get("cart") or [], ctx.branch_id)
            return {"answer": f"Done, your cart is updated. Subtotal: Rs {cart['subtotal']} "
                              f"(tax and delivery are added in the app at checkout).",
                    "items": [], "suggestions": ["Show my cart"], "handoff": False, "as_of": utc_now_iso()}
        sizes = [(p.get("item"), p.get("valid_sizes")) for r in state.get("tool_results") or []
                 for p in (r.get("problems") or []) if p.get("error") in ("size_required", "unknown_size")]
        cards = _which_one_cards(state.get("tool_results") or [])
        if sizes and sizes[0][1]:
            name, valid = sizes[0]
            options = ", ".join(f"{v['size']} Rs {v['price']}" if isinstance(v, dict) else str(v) for v in valid)
            return {"answer": f"{name or 'This item'} comes in different sizes ({options}). Which size would you like?",
                    "items": cards, "suggestions": [], "handoff": False, "as_of": utc_now_iso()}
        if cards:  # the cart code already knows the options: ask "which one?" with them, model or not
            return {"answer": WHICH_ONE.get(state.get("language"), WHICH_ONE["latin"]), "items": cards,
                    "suggestions": [], "handoff": False, "as_of": utc_now_iso()}
        return {"answer": "I couldn't update your cart just now. Please try again in a minute.",
                "items": [], "suggestions": ["Show my cart"], "handoff": False, "as_of": utc_now_iso()}
    # Nothing code can answer by itself: no guessed search results as cards, just an honest "try again".
    return {"answer": BUSY[_reply_lang(state)], "items": [], "suggestions": ["Show my cart"], "handoff": False,
            "as_of": utc_now_iso()}


def _model_name() -> str:
    model = {"ollama": settings.OLLAMA_MODEL, "openrouter": settings.OPENROUTER_MODEL}.get(settings.LLM_PROVIDER,
                                                                                         settings.GROQ_MODEL)
    planner = "" if settings.PLANNER_PROVIDER == settings.LLM_PROVIDER else f" (planner: {settings.PLANNER_PROVIDER})"
    return f"{settings.LLM_PROVIDER}:{model}{planner}"


def _trace(record: dict) -> None:
    TRACE_FILE.parent.mkdir(exist_ok=True)
    with TRACE_FILE.open("a") as f:
        f.write(json.dumps(redact_obj(record), ensure_ascii=False, default=str) + "\n")


# ---------- graph ----------

def build_graph(checkpointer=None):
    g = StateGraph(State)
    g.add_node("pre_checks", pre_checks)
    g.add_node("understand", understand_node)
    g.add_node("prefetch", prefetch)
    g.add_node("agent", agent)
    g.add_node("tools", tools)
    g.add_node("post_checks", post_checks)
    g.add_node("format_response", format_response)
    g.add_edge(START, "pre_checks")
    g.add_conditional_edges("pre_checks", route_after_pre_checks, ["understand", "format_response"])
    g.add_conditional_edges("understand", route_after_understand, ["prefetch", "format_response"])
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


def _run_config(thread_id: str, branch_id: str, user_id: Optional[str], payment_method: Optional[str] = None,
                order: Optional[dict] = None, tracked: Optional[List[dict]] = None) -> dict:
    return {"configurable": {"thread_id": thread_id, "user_id": user_id, "branch_id": branch_id,
                             "payment_method": payment_method, "order": order, "tracked": tracked or []},
            "recursion_limit": 25}


def _turn_input(message: str, cart: Optional[List[dict]]) -> dict:
    turn = {"messages": [HumanMessage(content=message)]}
    if cart is not None:  # None: keep the cart already in this thread (the CLI: one thread per cart)
        turn["cart"] = list(cart)
    return turn


def chat(message: str, *, thread_id: str, branch_id: str, user_id: Optional[str] = None,
         cart: Optional[List[dict]] = None, payment_method: Optional[str] = None, order: Optional[dict] = None,
         tracked: Optional[List[dict]] = None) -> dict:
    """One customer turn. user_id must come from verified auth, never from the model or the message.
    cart: the branch's current cart lines; the turn's resulting lines come back in response["cart_lines"].
    payment_method: the cart's chosen method; a new choice comes back in response["payment_method_set"].
    order: the order type ({"type", "lat"?, "lng"?}); a switch comes back in response["order_type_set"].
    tracked: orders placed from chat (newest first), so "where is my order?" finds the right one."""
    out = graph().invoke(_turn_input(message, cart),
                         config=_run_config(thread_id, branch_id, user_id, payment_method, order, tracked))
    return out["response"]


def chat_stream(message: str, *, thread_id: str, branch_id: str, user_id: Optional[str] = None,
                cart: Optional[List[dict]] = None, payment_method: Optional[str] = None,
                order: Optional[dict] = None, tracked: Optional[List[dict]] = None) -> Iterator[dict]:
    """Like chat(), but yields {"status": "..."} progress events as the turn runs, then {"response": {...}}."""
    final = None
    for mode, chunk in graph().stream(_turn_input(message, cart),
                                      config=_run_config(thread_id, branch_id, user_id, payment_method, order,
                                                         tracked),
                                      stream_mode=["custom", "values"]):
        if mode == "custom":
            yield chunk
        else:
            final = chunk
    yield {"response": final["response"]}
