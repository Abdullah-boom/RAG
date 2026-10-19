"""Understanding layer (L2) and workflow router (L3).

L2: one short model call with a JSON schema (no tools, no history beyond the bot's last reply) turns the message
into a Plan: what the customer wants (intent), the items they named with qty / size, a taste or kind in English
("khatta" -> "sour"), a budget, an order number, a payment method, and the language they wrote in.

L3: the Plan picks the workflow:
  - deterministic: the tool is certain from the intent (view cart, hours, deals, add "2 large Crown Crust",
    a price question, a craving). Code runs it and the model only words the reply, with no tool schemas.
  - agent: open-ended or risky to guess (remove from cart, clear the cart, chit-chat, unclear). The model calls
    tools itself, offered only the tools of that intent's topics.
  - off_topic: a fixed reply, unless the message has a business cue (then the agent; routing fails open).

When the call fails or returns nothing usable, plan_message returns None and the graph uses the word-list path
(intents.prefetch_tools), so this layer can only add understanding, never take the bot down.
"""
import json
import re
from typing import List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError

from app import cart
from app.agent import intents
from app.retrieval import GENERIC_WORDS

Intent = Literal["item_info", "browse", "recommend", "deals", "add", "remove", "view_cart", "clear_cart",
                 "payment_options", "choose_payment", "checkout", "branch", "order_status", "order_history",
                 "app_help", "off_topic", "chat", "unclear"]


class PlanItem(BaseModel):
    name: str = Field(..., max_length=80)
    qty: Optional[int] = Field(None, ge=1, le=cart.MAX_QTY)
    size: Optional[str] = Field(None, max_length=20)


class Plan(BaseModel):
    intent: Intent
    items: List[PlanItem] = Field(default_factory=list, max_length=6)
    wish: Optional[str] = Field(None, max_length=60)
    budget: Optional[int] = Field(None, ge=1, le=100000)
    order_no: Optional[str] = Field(None, max_length=40)
    payment: Optional[str] = Field(None, max_length=60)
    language: Literal["english", "roman_urdu", "urdu_script"] = "english"


def schema() -> dict:
    """The JSON schema the model's output is held to (Ollama `format`), without pydantic's titles."""
    s = Plan.model_json_schema()

    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items() if k != "title"}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x
    return strip(s)


PROMPT = """You read one customer message for the Cheezious fast-food app (pizzas, burgers, wings, pasta, deals) and
output JSON only. Do not answer the customer.

intent, one of:
- item_info: asks about a NAMED item: price, sizes, what is in it, do you have it ("bazinga kitne ka hai")
- browse: wants to see a kind of food or the menu ("burgers dikhao", "which pizzas do you have")
- recommend: a craving, taste, mood or "something good" ("kuch khatta", "bhook lagi hai", "what's popular")
- deals: deals, combos, offers, discounts
- add: put items in the cart, or picks an option the bot just showed to order it ("pehli wali", "large wala")
- remove: take something out of the cart
- view_cart: see the cart or its total
- clear_cart: empty the whole cart
- payment_options: how they can pay
- choose_payment: tells which payment method they will use ("cash on delivery karunga")
- checkout: wants to place / confirm the order now
- branch: branch open or closed, timings, delivery or pickup, minimum order
- order_status: where is my order, status of an order they already placed
- order_history: their past orders
- app_help: using the app, account, login, vouchers, refunds, complaints
- off_topic: nothing to do with Cheezious or food (maths, weather, news, coding, jokes, other companies)
- chat: talk about the conversation itself, the bot, a complaint about an answer
- unclear: none of the above fits, or you are not sure

Fields (leave out or null what the message does not say; never guess):
- items: each item the customer named, with "name" in their own words (keep spelling), "qty" and "size"
  only if they said them. If they point at ONE item the bot just showed ("pehli wali", "the second one"),
  use only that item's name from SHOWN. size is only a size word they typed (Small, Regular, Large, Party).
- A question about a price or an item ("pehli wali kitne ki hai?") is item_info, never add.
- wish: for recommend / browse / app_help, short ENGLISH keywords; translate Urdu taste words (teekha =
  spicy, khatta = sour, meetha = sweet, namkeen = salty): "sour", "spicy chicken", "sweet dessert", "burger",
  "change password". For a group ("4 log hain") use "deal".
- budget: a number of rupees only if the customer wrote one ("800 se kam" -> 800).
- order_no, payment: as written.
- language: english, roman_urdu (Urdu in English letters) or urdu_script.

Examples:
"2 bazinga aur 1 large crown crust daal do" -> {{"intent":"add","items":[{{"name":"bazinga","qty":2}},{{"name":"crown crust","qty":1,"size":"Large"}}],"language":"roman_urdu"}}
"kuch meetha hai?" -> {{"intent":"recommend","wish":"sweet dessert","language":"roman_urdu"}}
"chicken tikka pizza kitne ka hai" -> {{"intent":"item_info","items":[{{"name":"chicken tikka pizza"}}],"language":"roman_urdu"}}
"4 log hain kya order karein" -> {{"intent":"recommend","wish":"deal","language":"roman_urdu"}}
"2+2 kitne hote hain" -> {{"intent":"off_topic","language":"roman_urdu"}}
"is your branch open?" -> {{"intent":"branch","language":"english"}}

BOT'S LAST REPLY: {last_reply}
SHOWN: {shown}
CUSTOMER: {message}
JSON:"""


def prompt(message: str, last_reply: str = "", offered: Tuple[dict, ...] = ()) -> str:
    shown = "; ".join(f"{n}. {o['name']}" + (f" ({o['type']})" if o.get("type") else "")
                      for n, o in enumerate(offered, 1)) or "nothing"
    last = " ".join((last_reply or "").split())[:300] or "none"
    return PROMPT.format(last_reply=last, shown=shown, message=message[:400])


JSON_OBJECT = re.compile(r"\{.*\}", re.S)


def parse(text: str) -> Optional[Plan]:
    """The model's output as a Plan, or None. Tolerates a code fence or words around the JSON object."""
    m = JSON_OBJECT.search(text or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    # Small models write "null"/"" for fields they leave out, and a bare string for an item.
    data = {k: v for k, v in data.items() if v not in (None, "", "null", "None", [])}
    data["items"] = [i if isinstance(i, dict) else {"name": str(i)} for i in data.get("items") or []]
    data["items"] = [{k: v for k, v in i.items() if v not in (None, "", "null", "None", 0)}
                     for i in data["items"] if i.get("name")]
    try:
        return Plan.model_validate(data)
    except ValidationError:
        return None


def missing_item(query: str, items: List[dict], corrections: dict) -> bool:
    """The customer named something ("sushi") and none of the items found has any of its distinctive words, even
    after spelling fixes ("tiqqa" -> "tikka"): we don't have it, and the results are only nearest neighbours."""
    said = {w for w in re.findall(r"[a-z]+", query.lower()) if len(w) > 2 and w not in GENERIC_WORDS}
    said |= {corrections[w] for w in list(said) if w in corrections}
    if not said or not items:
        return False
    names = {w for i in items for w in re.findall(r"[a-z]+", (i.get("name") or "").lower())}
    return not said & names


# ---------- L3: the workflow for a plan ----------

INTENT_TOPICS = {
    "item_info": ["menu"], "browse": ["menu"], "recommend": ["menu"], "deals": ["menu"],
    "add": ["menu", "cart"], "remove": ["menu", "cart"], "view_cart": ["menu", "cart"], "clear_cart": ["menu", "cart"],
    "payment_options": ["menu", "cart", "payment"], "choose_payment": ["menu", "cart", "payment"],
    "checkout": ["menu", "cart", "payment", "checkout"], "branch": ["menu", "branch"],
    "order_status": ["menu", "orders"], "order_history": ["menu", "orders"], "app_help": ["menu", "faq"],
}
CALL = Tuple[str, dict]


def _search(query: str, plan: Plan) -> CALL:
    args = {"query": query[:200]}
    if plan.budget:
        args["max_price"] = plan.budget
    return "search_menu", args


# Not an item: the model sometimes lists the customer's verb or a vague word as one ("4 log ... order" -> "order").
NOT_ITEMS = {"order", "orders", "item", "items", "food", "khana", "khane", "kuch", "something", "anything", "it",
             "this", "that", "cart", "menu", "deal", "meal"}
# Wanting something, beyond intents.ADD: "2 bazinga chahiye", "I'll have a Crown Crust", "ek pizza de do".
WANT = re.compile(r"\b(chahiye|chahye|chaiye|lena|lunga|lungi|loon|lun|de\s*do|dedo|dein|mangwa\w*|bhej\w*|i'?ll\s+"
                  r"(have|take)|i\s+want|give\s+me|get\s+me|order\s+kar\w*)\b", re.I)


def _size(size: Optional[str], said: str) -> Optional[str]:
    """A size the customer actually typed ("large"), never one the model filled in ("Pizza", "(no size)")."""
    low = f" {said.lower()} "
    words = re.findall(r"[a-z]+", (size or "").lower())
    if words and all(w in cart.SIZE_WORDS for w in words) and any(f" {w} " in low for w in words):
        return " ".join(words).capitalize()
    pcs = re.fullmatch(r"\s*(\d+)\s*(pcs?|pieces?)\s*", size or "", re.I)
    return f"{pcs.group(1)} Pcs" if pcs and pcs.group(1) in said else None


def _add_items(plan: Plan, said: str, offered: Tuple[dict, ...]) -> Optional[CALL]:
    """An add only with real evidence for it; what is pointed at is resolved by the cart code, never the model."""
    pointed = cart.pointed_item(said, offered) if offered else None
    if pointed or (offered and (intents.SIZE_ONLY.match(said) or cart.chosen_from_offered(said, offered))):
        return intents.add_call(said)  # "dusri wali", "large", "Crown Crust wala" after a list
    if not (intents.ADD.search(said) or WANT.search(said)):
        return None
    items = []
    for it in plan.items:
        if it.name.strip().lower() in NOT_ITEMS:
            continue
        line = {"name": it.name, "qty": it.qty or 1}
        size = _size(it.size, said)
        if size:
            line["size"] = size
        items.append(line)
    return ("add_to_cart", {"items": items}) if items else None


def route(plan: Plan, said: str, offered: Tuple[dict, ...] = ()) -> Tuple[List[CALL], List[str]]:
    """(calls the code runs now, topics). No calls = the agent decides, with only these topics' tools.
    Topics ALL = everything (chat / unclear)."""
    i, calls = plan.intent, []
    if i == "add" and intents.QUESTION.search(said) and intents.PRICE.search(said) \
            and not (intents.ADD.search(said) or WANT.search(said)):
        i = "item_info"  # "pehli wali kitne ki hai?": a question about it, not adding it
    topics = list(INTENT_TOPICS.get(i, intents.ALL_TOPICS))
    if i == "item_info":
        pointed = cart.pointed_item(said, offered) if offered else None
        if pointed:  # the cart code's reading of "pehli wali", not the model's
            calls = [("search_menu", {"query": pointed["name"], "picked": True})]
        else:
            names = [it.name for it in plan.items if it.name.strip().lower() not in NOT_ITEMS] or \
                ([plan.wish] if plan.wish else []) or ([intents.price_query(said)] if intents.price_query(said) else [])
            calls = [_search(n, plan) for n in names[:intents.MAX_PREFETCH]]
    elif i == "browse":
        # The model sometimes names nothing for "burgers dikhao": the menu words in the message, then best sellers.
        what = plan.wish or " ".join(it.name for it in plan.items) or intents.price_query(said)
        calls = [_search(what, plan)] if what else [("get_best_sellers", {})]  # "dikha" with nothing named
    elif i == "recommend":
        if plan.wish and plan.wish.strip().lower() in ("deal", "deals", "combo", "family deal"):
            calls = [("search_menu", {"query": "deal", "max_price": plan.budget, "deals_only": True})
                     if plan.budget else ("get_deals", {})]
        elif plan.wish:
            calls = [_search(plan.wish, plan)]
        elif plan.budget:
            calls = [_search("popular", plan)]
        else:
            calls = [("get_best_sellers", {})]
    elif i == "deals":
        calls = [("search_menu", {"query": plan.wish or "deal", "max_price": plan.budget, "deals_only": True})
                 if plan.budget else ("get_deals", {})]
    elif i == "add":
        call = _add_items(plan, said, offered)
        calls = [call] if call else []
        if not call:  # no evidence for an add: the agent decides, with the menu and cart tools
            topics = ["menu", "cart"]
    elif i == "view_cart":
        calls = [("view_cart", {})]
    elif i == "payment_options":
        calls = [("get_payment_methods", {})]
    elif i == "choose_payment":
        calls = [("set_payment_method", {"method": (plan.payment or said)[:60]})]
    elif i == "checkout":
        calls = [("review_order", {})]
    elif i == "branch":
        calls = [("check_branch_status", {})]
    elif i == "order_status":
        calls = [("get_order_status", {"order_no": plan.order_no} if plan.order_no else {})]
    elif i == "order_history":
        calls = [("get_order_history", {})]
    elif i == "app_help":
        calls = [("search_faq", {"query": (plan.wish or said)[:200]})]
    # remove / clear_cart / chat / unclear: the agent, with this intent's tools only.
    return calls[:intents.MAX_PREFETCH], topics
