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

from app import cart, order_flow, payments
from app.agent import intents
from app import retrieval
from app.retrieval import GENERIC_WORDS

Intent = Literal["item_info", "browse", "recommend", "deals", "add", "remove", "view_cart", "clear_cart",
                 "payment_options", "choose_payment", "order_type", "checkout", "branch", "order_status", "order_history",
                 "app_help", "off_topic", "chat", "negotiate", "complain", "change_qty", "affirm", "deny", "unclear"]


class PlanItem(BaseModel):
    name: str = Field(..., max_length=80)
    qty: Optional[int] = Field(None, ge=1, le=cart.MAX_QTY)
    size: Optional[str] = Field(None, max_length=20)


class Plan(BaseModel):
    intent: Intent
    items: List[PlanItem] = Field(default_factory=list, max_length=6)
    wish: Optional[str] = Field(None, max_length=60)
    wish_said: Optional[str] = Field(None, max_length=60)  # the customer's own describing words, as typed
    avoid: Optional[str] = Field(None, max_length=60)  # what they DON'T want, in English ("teekha na ho" -> "spicy")
    wish_sure: Optional[bool] = None  # False: the model doesn't know what those words mean (ask, don't guess)
    budget: Optional[int] = Field(None, ge=1, le=100000)
    order_no: Optional[str] = Field(None, max_length=40)
    payment: Optional[str] = Field(None, max_length=60)
    order_type: Optional[Literal["delivery", "pickup"]] = None
    language: Literal["english", "roman_urdu", "urdu_script"] = "english"

    def unsure(self) -> bool:
        """The customer's describing words have no meaning we can search for. Judged by what the model did, not
        only its flag (Llama 3.1 8B flags "teekha" unsure while translating it to "spicy"): unsure when it gave
        no meaning, or just repeated the words untranslated ("gol gol" -> "gol gol")."""
        said = " ".join(re.findall(r"\w+", (self.wish_said or "").lower()))
        wish = " ".join(re.findall(r"\w+", (self.wish or "").lower()))
        return self.wish_sure is False and (not wish or wish == said)


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
- negotiate: asks for a lower price or a discount for themselves ("300 kam kar do", "give me 50% off")
- add: put items in the cart, or picks an option the bot just showed to order it ("pehli wali", "large wala")
- remove: take something out of the cart
- change_qty: changes how many of something ALREADY IN THE CART ("make it just 1", "2 kar do", "ek aur")
- view_cart: see the cart or its total ("kitne ka hua sab?", "total kitna hua")
- clear_cart: empty the whole cart
- payment_options: how they can pay
- choose_payment: tells which payment method they will use ("cash on delivery karunga")
- order_type: wants delivery or pickup for their order, or to switch ("pickup kar do", "make it delivery",
  "main khud le lunga")
- checkout: wants to place / confirm the order now
- branch: branch open or closed, timings, whether it offers delivery or pickup, minimum order
- order_status: where is my order, status of an order they already placed
- order_history: their past orders
- app_help: using the app, account, login, vouchers, refunds, complaints
- off_topic: a task or question with nothing to do with Cheezious or food (maths, weather, news, coding, other
  companies)
- chat: greeting-like small talk with the bot ("aur sunao", "kaise ho", "how's your day?", "kya haal hai").
  Small talk is chat, never off_topic
- complain: a bad experience, a complaint or a refund ("pizza thanda tha", "I want a refund")
- affirm / deny: only says yes / no ("haan ji", "ok", "nahi") without naming anything
- unclear: none of the above fits, or you are not sure

Fields (leave out or null what the message does not say; never guess):
- items: each item the customer named, with "name" in their own words (keep spelling), "qty" and "size"
  only if they said them. If they wrote in Urdu script, write the name in English letters as it would appear on
  an English menu ("کراؤن کرسٹ" -> "crown crust", "زنگر برگر" -> "zinger burger"). If they point at ONE item the bot just showed ("pehli wali", "the second one"),
  use only that item's name from SHOWN. size is only a size word they typed (Small, Regular, Large, Party).
- A question about a price or an item ("pehli wali kitne ki hai?") is item_info, never add.
- wish: for recommend / browse / app_help, the LITERAL English meaning of the customer's own describing words,
  as short keywords ("spicy chicken", "burger", "change password"). Translate word for word; never swap in a
  taste, dish or category the customer did not say. For a group ("4 log hain") use "deal".
- wish_said: those describing words exactly as the customer typed them ("teekha", "gol gol").
- avoid: what the customer does NOT want, in English ("jo teekha na ho" -> "spicy"); then wish holds only what
  they do want, or nothing.
- wish_sure: false when you do not really know what the customer's describing words mean, or they could mean
  several different things. Then still fill wish_said; do not guess a wish.
- budget: a number of rupees only if the customer wrote one ("800 se kam" -> 800).
- order_no, payment: as written. order_type: "delivery" or "pickup", the one they want now.
- language: english, roman_urdu (Urdu in English letters) or urdu_script.

Examples:
"2 bazinga aur 1 large crown crust daal do" -> {{"intent":"add","items":[{{"name":"bazinga","qty":2}},{{"name":"crown crust","qty":1,"size":"Large"}}],"language":"roman_urdu"}}
"kuch meetha hai?" -> {{"intent":"recommend","wish":"sweet","wish_said":"meetha","language":"roman_urdu"}}
"koi lambi si cheez" -> {{"intent":"recommend","wish":"long","wish_said":"lambi si","language":"roman_urdu"}}
"kuch dhinchak sa do" -> {{"intent":"recommend","wish_said":"dhinchak","wish_sure":false,"language":"roman_urdu"}}
"chicken tikka pizza kitne ka hai" -> {{"intent":"item_info","items":[{{"name":"chicken tikka pizza"}}],"language":"roman_urdu"}}
"4 log hain kya order karein" -> {{"intent":"recommend","wish":"deal","wish_said":"4 log","language":"roman_urdu"}}
"2+2 kitne hote hain" -> {{"intent":"off_topic","language":"roman_urdu"}}
"is your branch open?" -> {{"intent":"branch","language":"english"}}
"aur sunao" -> {{"intent":"chat","language":"roman_urdu"}}
"bachon ke liye kuch jo teekha na ho" -> {{"intent":"recommend","avoid":"spicy","wish_said":"teekha na ho","language":"roman_urdu"}}
"actually make it just 1" (CART: 2 x Crown Crust) -> {{"intent":"change_qty","items":[{{"name":"Crown Crust","qty":1}}],"language":"english"}}
"delivery se pickup kar do" -> {{"intent":"order_type","order_type":"pickup","language":"roman_urdu"}}
"2 لارج کراؤن کرسٹ ایڈ کر دو" -> {{"intent":"add","items":[{{"name":"crown crust","qty":2,"size":"Large"}}],"language":"urdu_script"}}

RECENT CONVERSATION (oldest first): {recent}
CART: {cart}
BOT'S LAST REPLY: {last_reply}
SHOWN: {shown}
CUSTOMER: {message}
JSON:"""


def prompt(message: str, last_reply: str = "", offered: Tuple[dict, ...] = (), cart_lines: Tuple[dict, ...] = (),
           recent: Tuple[Tuple[str, str], ...] = ()) -> str:
    """The message with its conversation state: the planner used to see only the bot's last reply, so "kitne ka hua
    sab?" (the cart total) and "make it just 1" (the item just added) had nothing to refer to."""
    shown = "; ".join(f"{n}. {o['name']}" + (f" ({o['type']})" if o.get("type") else "")
                      for n, o in enumerate(offered, 1)) or "nothing"
    last = " ".join((last_reply or "").split())[:300] or "none"
    cart_text = "; ".join(f"{l['qty']} x {l['name']}" + (f" ({l['size']})" if l.get("size") else "")
                          for l in cart_lines) or "empty"
    turns = " | ".join(f"{who}: {' '.join(text.split())[:150]}" for who, text in recent) or "none"
    return PROMPT.format(last_reply=last, shown=shown, message=message[:400], cart=cart_text, recent=turns)


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


def _stem(word: str) -> str:
    """Loose stem, so "cheesy" finds "cheese" and "crispy" finds "crisp": the first letters, at least four."""
    return word[:max(4, len(word) - 2)]


WISH_FILLER = {"something", "anything", "item", "items", "food", "dish", "dishes", "thing", "things", "with", "and",
               "the", "for", "some", "kind", "type", "like", "very",
               "kuch", "koi", "sa", "si", "wala", "wali", "chahiye", "cheez", "hai", "dikhao",  # Roman Urdu filler
               "kids", "children", "child", "bachon", "bachay", "family", "people", "person", "log"}  # who, not what


NEGATION = re.compile(r"\b(?:not|no|without|non|less|avoid)\s*-?\s*([a-z]+)", re.I)


def grounded(wish: str, items: List[dict], avoid: Optional[str] = None) -> List[dict]:
    """Items that really fit a description: every content word of the wish appears (loosely) in the item's name,
    description, category or type, and no word of what they want to avoid does ("not spicy", avoid="spicy"). A
    vector search always returns its nearest neighbours, even for something the menu has nothing of ("round");
    those must not be offered as matches."""
    wish = (wish or "").lower()
    excluded = [_stem(w) for w in NEGATION.findall(wish) + re.findall(r"[a-z]+", (avoid or "").lower())
                if len(w) > 2 and w not in WISH_FILLER]
    wish = NEGATION.sub(" ", wish)
    stems = [_stem(w) for w in re.findall(r"[a-z]+", wish) if len(w) > 2 and w not in WISH_FILLER]
    if not stems and not excluded:
        return items

    def fits(item: dict) -> bool:
        text = " ".join(str(item.get(k) or "") for k in ("name", "description", "category", "type")).lower()
        tokens = re.findall(r"[a-z]+", text)
        has = lambda s: any(t.startswith(s) for t in tokens)
        return all(has(s) for s in stems) and not any(has(s) for s in excluded)
    return [i for i in items if fits(i)]


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
    "checkout": ["menu", "cart", "payment", "checkout"], "order_type": ["menu", "cart", "payment", "checkout"],
    "branch": ["menu", "branch"],
    "chat": ["chat"],  # greeting-like small talk: a fixed greet-and-ask reply, no tools, no model
    # Fixed replies too (app/agent/behaviour.py): a complaint never gets a promise; a bare yes / no gets a question.
    "complain": ["complain"], "affirm": ["affirm"], "deny": ["deny"],
    "negotiate": ["menu"], "change_qty": ["menu", "cart"],
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
                  r"(have|take)|i\s+want|give\s+me|get\s+me|order\s+kar\w*)\b|چاہیے|دے\s+دو|منگوا", re.I)


def _size(size: Optional[str], said: str) -> Optional[str]:
    """A size the customer actually typed ("large"), never one the model filled in ("Pizza", "(no size)")."""
    low = f" {said.lower()} "
    words = re.findall(r"[a-z]+", (size or "").lower())
    if URDU_LETTERS.search(said) and words and all(w in cart.SIZE_WORDS for w in words):
        return " ".join(words).capitalize()  # "لارج": the planner's reading is all we can compare
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
    said_qty = _qty_said(said) if len([it for it in plan.items if it.name.strip().lower() not in NOT_ITEMS]) == 1 \
        else None  # one item: the number the customer typed wins (the planner turned "2 large" into 1)
    for it in plan.items:
        if it.name.strip().lower() in NOT_ITEMS:
            continue
        line = {"name": it.name, "qty": said_qty or it.qty or 1}
        size = _size(it.size, said)
        if size:
            line["size"] = size
        items.append(line)
    return ("add_to_cart", {"items": items}) if items else None


URDU_LETTERS = re.compile(r"[\u0600-\u06FF]")


def _in_words(name: str, said: str) -> bool:
    """The customer's message has (loosely) a distinctive word of this name: they said it, the model didn't add it.
    Urdu script can't be compared letter by letter with English menu names ("کراؤن کرسٹ" = Crown Crust): there the
    planner's reading is the only one we have."""
    if URDU_LETTERS.search(said):
        return True
    said_words = re.findall(r"\w+", said.lower())
    name_words = [w for w in re.findall(r"\w+", name.lower()) if len(w) > 2 and w not in GENERIC_WORDS]
    return any(_stem(n).startswith(_stem(w)) or _stem(w).startswith(_stem(n))
               for n in name_words for w in said_words if len(w) > 2)


def _points(said: str, offered: Tuple[dict, ...]) -> bool:
    return bool(offered and (cart.pointed_item(said, offered) or intents.SIZE_ONLY.match(said)
                             or cart.chosen_from_offered(said, offered)))


FIXED_REPLY = ("chat", "complain", "affirm", "deny")  # answered by app/agent/behaviour.py, no tools, no model
MORE = re.compile(r"\b(aur|more|another|extra|zyada)\b", re.I)


def _qty_said(said: str) -> Optional[int]:
    """A number the customer typed ("make it just 1", "2 kar do", "ek aur")."""
    m = re.search(r"\b(\d{1,2})\b(?!\s*(?:pcs?|pieces?|ltr|lit(?:re|er)s?|inch|ml)\b)", said, re.I)  # "6 pcs" = a size
    if m:
        return int(m.group(1))
    return next((n for w, n in intents.NUMBER_WORDS.items() if re.search(rf"\b{w}\b", said, re.I)), None)


def _change_qty(plan: Plan, said: str, cart_lines: Tuple[dict, ...]) -> Optional[CALL]:
    """Change an item already in the cart: the line the customer names, else the last one added. "ek aur" adds to
    it; a plain number sets it; 0 removes it. None: nothing in the cart, or no number said."""
    if not cart_lines:
        return None
    named = [n for n, l in enumerate(cart_lines, 1) if _in_words(l["name"], said)
             or any(_in_words(l["name"], it.name) for it in plan.items)]
    line_no = named[-1] if named else len(cart_lines)
    qty = _qty_said(said) or next((it.qty for it in plan.items if it.qty), None)
    if qty is None:
        return None
    if MORE.search(said):
        qty = cart_lines[line_no - 1]["qty"] + qty
    return ("remove_from_cart", {"line_no": line_no}) if qty == 0 else \
        ("set_quantity", {"line_no": line_no, "qty": min(qty, cart.MAX_QTY)})


# "jo teekha na ho", "without onion", "no spicy", "bina mirchi": what the customer does not want. The planner often
# leaves avoid empty; the word comes from their message and its meaning from the menu synonyms (teekha -> spicy, hot).
NEGATED = re.compile(r"\b([a-z]+)\s+(?:na|nah|nahi|nahin)\s+ho\b|\b(?:no|not|without|bina|non)[\s-]+([a-z]+)", re.I)


def _avoid_said(said: str) -> Optional[str]:
    words = [w.lower() for pair in NEGATED.findall(said) for w in pair if w and w.lower() not in WISH_FILLER]
    if not words:
        return None
    synonyms = retrieval._synonyms()
    out = []
    for w in words:
        out += [w] + [s for s in synonyms.get(w) or [] if " " not in s]  # single words: "crown crust" isn't a taste
    return " ".join(dict.fromkeys(out))


def route(plan: Plan, said: str, offered: Tuple[dict, ...] = (),
          cart_lines: Tuple[dict, ...] = ()) -> Tuple[List[CALL], List[str]]:
    """(calls the code runs now, topics). No calls = the agent decides, with only these topics' tools.
    Topics ALL = everything (unclear). cart_lines: the cart now, for "make it just 1"."""
    i, calls = plan.intent, []
    if i in ("recommend", "browse") and not plan.avoid and _avoid_said(said):
        plan = plan.model_copy(update={"avoid": _avoid_said(said)})
    if i == "order_type" and not intents.TYPE_WORD.search(said):
        # The planner set an order type the customer never said ("actually make it just 1" -> delivery).
        i = "change_qty" if cart_lines and _qty_said(said) is not None else "unclear"
    # Act only on what the customer said: the model lists items from the screen ("aur sunao" -> "Hot n Spicy"),
    # and reads "300 kam kar do" (take 300 off) as a budget: a budget only when their words set a spending limit.
    plan = plan.model_copy(update={"items": [it for it in plan.items if _in_words(it.name, said)],
                                   "budget": plan.budget if intents.BUDGET.search(said) else None})
    if i in ("add", "item_info", "remove") and not plan.items and not plan.wish and not _points(said, offered) \
            and not intents.price_query(said) and not (intents.ADD.search(said) or WANT.search(said)):
        # nothing named or pointed at: a bare yes / no, or conversation; never an action on a guessed item
        i = {"yes": "affirm", "no": "deny"}.get(order_flow.answer_to_confirm(said), "chat")
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
        if (plan.wish_said or plan.wish) and plan.unsure():
            calls = [("get_best_sellers", {})]  # meaning unclear: ask what they mean, best sellers as a lead
        elif plan.wish and plan.wish.strip().lower() in ("deal", "deals", "combo", "family deal"):
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
        # The customer's words first: the model rewrote "online card" as "debit/credit card", which is ambiguous.
        own = payments.METHOD_WORDS & set(re.findall(r"[a-z]+", said.lower()))
        calls = [("set_payment_method", {"method": (said if own else plan.payment or said)[:60]})]
    elif i == "negotiate":
        calls = [("get_deals", {})]  # prices are fixed (the reply opens by saying so); deals are the real savings
    elif i == "change_qty":
        call = _change_qty(plan, said, cart_lines)
        calls = [call] if call else []
        if not call:
            topics = ["menu", "cart"]
    elif i == "checkout":
        calls = [("review_order", {})]
    elif i == "order_type":
        # The customer's own words decide (the model sometimes fills the type they are leaving); its field is
        # only the fallback when the words name one type alone.
        call = intents.order_type_call(said) or (("set_order_type", {"order_type": plan.order_type})
                                                 if plan.order_type else None)
        calls = [call] if call else []
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
