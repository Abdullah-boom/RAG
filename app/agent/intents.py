"""Common questions whose tool is obvious from the words alone, in English, Roman Urdu and Urdu script.

The agent runs these tools BEFORE the model writes, so even a small local model answers from live data
instead of from memory (it often skipped them: "I can't see your cart", invented hours). The model still
writes the reply, in the customer's language, and can call more tools. Only argument-free tools are
pre-run, plus a menu search for plain price questions ("pizza kitni ki hai?", "how much is chicken tikka")
where the item is simply what's left after the question words. Anything else that needs the customer's words
turned into arguments (add to cart, open-ended searches) stays with the model.
"""
import re
from typing import List, Optional, Tuple

from app import cart
from app.retrieval import index
from app.agent.guards import ROMAN_URDU

_W = r"(?:^|\b)"

# Changing the cart needs arguments (what, how many): leave those to the model, don't pre-run view_cart.
CART_CHANGE = re.compile(
    _W + r"(add|daal|dal\s*do|dalo|daalo|remove|hata|hatao|nikal|nikalo|clear|khali|empty|delete|cancel)\b", re.I)
CART_VIEW = re.compile(
    _W + r"((my|mera|meri|mere)\s+(cart|kart|basket|total)"
    r"|cart\s+(me|mein|main|mai|men|dikhao|dikha|dikhado|show|kya|check)"
    r"|(show|view|see|check|open)\s+(the\s+|my\s+)?cart"
    r"|in\s+(my|the)\s+cart"
    r"|total\s+(kitna|kya|how\s+much)|(kitne|kitna)\s+ka\s+hua|kitna\s+hua|(sab|total)\s+kitne?\s+ka"
    r"|how\s+much\s+is\s+(my\s+|the\s+)?total)\b"
    r"|کارٹ",
    re.I)
BRANCH = re.compile(
    _W + r"(open|opened|close|closed|closing|timings?|hours|deliver|delivers|delivery|pick\s*up|dine[\s-]?in"
    r"|minimum\s+order|khula|khuli|khulta|khulti|band|kab\s+tak|kitne\s+baje|kab\s+khul\w*)\b"
    r"|کھل|بند|ڈیلیوری|اوقات",
    re.I)
DEALS = re.compile(_W + r"(deals?|offers?|combos?|discounts?|promo|promotions?)\b|ڈیل|آفر", re.I)
BEST = re.compile(
    _W + r"(best\s*sellers?|best\s*selling|popular|most\s+(popular|ordered|sold)|famous|recommend\w*|suggest\w*"
    r"|what\s+should\s+i\s+(eat|order|get|try)"
    r"|(kya|kiya)\s+(khaun|khaon|khilaon|khilaun|khilau|order\s+kar(un|oon)|lun|loon|mangwa(un|oon))"
    r"|zyada\s+bikne|sab\s*se\s+(acha|achi|zyada))\b",
    re.I)

# Adding one thing to the cart: the cart code decides what (named / pointed at / "which one?"), not the model.
ADD = re.compile(_W + r"(add|daal|daalo|dalo|dal\s+do|daal\s+do|dal\s+den|daal\s+dein|put|include"
                 r"|laga\s+do|lga\s+do|lagao|laga\s+dein|laga\s+den)\b|ایڈ|ڈال\s*د|شامل\s+کر"
                 r"|cart\s+(mein|me|main)\s+(daal|dal|add)", re.I)
NOT_ADD = re.compile(_W + r"(remove|hata|hatao|nikal|nikalo|clear|khali|empty|delete|cancel)\b", re.I)
SEVERAL = re.compile(r"\b(aur|and|plus|&)\b|,", re.I)  # "2 bazinga aur 1 pasta": the model splits these
NUMBER_WORDS = {"one": 1, "ek": 1, "aik": 1, "two": 2, "three": 3, "teen": 3, "four": 4, "char": 4, "chaar": 4,
                "five": 5, "panch": 5, "paanch": 5, "six": 6, "che": 6, "chay": 6}
ADD_FILLER = re.compile(_W + r"(add|daal|daalo|dalo|dal|daal|do|den|dein|put|include|to|in|into|my|the|a|an|cart"
                        r"|mein|me|main|please|plz|pls|bhi|bhai|yaar|karo|kar|kardo|kr|krdo|laga|lga|lagao)\b", re.I)
# "pehli wali", "the second one", "option 2", "large" (after "which size?"): pointing at what was just shown.
POINT = re.compile(_W + r"(first|second|third|fourth|fifth|last|1st|2nd|3rd|4th|5th|pehla|pehli|pehle|dusra|dusri"
                   r"|doosra|doosri|teesra|teesri|aakhri|akhri|isko|usko|yehi|wohi|option\s*\d|number\s*\d)\b", re.I)
# "which burger did you mention first?" points at nothing: a question, not a choice (unless it also says add).
QUESTION = re.compile(_W + r"(which|what|whats|what's|who|when|where|why|how|did|does|do\s+you|kya|kia|kaun|kon"
                      r"|konsa|kaunsa|konsi|kaunsi|kab|kyun|kyon|kaise|kesa|kitna|kitni|kitne)\b", re.I)
SIZE_ONLY = re.compile(r"^\s*(small|regular|medium|large|party|family)(\s+(size|wala|wali|one|please|chahiye))?\s*[.!]?\s*$",
                       re.I)


def add_call(message: str) -> Tuple[str, dict]:
    """add_to_cart with the customer's own words as the name, and the quantity / size they typed."""
    qty = next((int(w) for w in re.findall(r"\b\d{1,2}\b", message) if 0 < int(w) <= cart.MAX_QTY), None) or \
        next((NUMBER_WORDS[w] for w in re.findall(r"[a-z]+", message.lower()) if w in NUMBER_WORDS), 1)
    size = next((w.capitalize() for w in re.findall(r"[a-z]+", message.lower())
                 if w in {"small", "regular", "medium", "large", "party", "family"}), None)
    name = " ".join(re.sub(r"\d+", " ", ADD_FILLER.sub(" ", message)).split()) or message
    item = {"name": name[:80], "qty": qty}
    if size:
        item["size"] = size
    return "add_to_cart", {"items": [item]}


# "Something good?" asked loosely: a quality word plus something that makes it a request.
QUALITY = re.compile(
    _W + r"(acha|achi|achay|achey|achha|achhi|accha|acchi|behtar|behtareen|mazay\w*|maze\w*|mazedar|mazaydar"
    r"|lazeez|lazeez\w*|zabardast|tasty|delicious|yummy|good|nice|great|special)\b|اچھ|مزیدار|لذیذ", re.I)
GREETING = re.compile(_W + r"good\s+(morning|evening|night|afternoon|day)\b", re.I)
ASK = re.compile(
    _W + r"(cheez|cheezein|chiz|chez|kuch|kch|koi|kya|kia|konsa|kaunsa|konsi|kaunsi|batao|btao|bata|bataen|bataein"
    r"|dikhao|something|anything|item"
    r"|items|what|whats|what's|khana|khane|order)\b|چیز|بتائ|کچھ", re.I)


def wants_recommendation(message: str) -> bool:
    if BEST.search(message):
        return True
    text = GREETING.sub(" ", message)
    return bool(QUALITY.search(text) and ASK.search(text))


PAYMENT = re.compile(
    _W + r"(pay|paying|payment|payments|cash|cod|card|cards|online|easypaisa|easy\s*paisa|jazzcash|jazz\s*cash"
    r"|wallet|debit|credit|adaigi|adayegi|paise\s+kaise)\b|ادائیگی|کیش|کارڈ", re.I)


# The customer is choosing, not asking ("cash on delivery karunga", "I'll pay by card"). The tool then checks
# the words name exactly one method this branch offers; if not, it returns the options and the model asks.
CHOICE = re.compile(
    _W + r"(karunga|karungi|karoonga|karun|karoon|karenge|kardo|kar\s+do|rakh\s+do|rakhna|rakhen|chahiye"
    r"|i'll|i\s+will|will\s+pay|i\s+want|i'd\s+like|go\s+with|choose|select|pay\s+by|pay\s+with|using)\b", re.I)
PAYMENT_PHRASE = re.compile(r"\b(cash|card|pay\w*)\s+on\s+(delivery|pickup)\b", re.I)  # not a delivery question


# Switching between delivery and pickup ("pickup kar do", "make it delivery", "I'll collect it myself"); a
# question about the service ("do you deliver to G-11?") has no switching word and stays a branch question.
TYPE_WORD = re.compile(r"\b(?P<pickup>pick[\s-]*up|take[\s-]*away|collect|self[\s-]*pick\w*|khud\s+(le|aa|utha)\w*)\b"
                       r"|\b(?P<delivery>delivery|deliver|home\s+delivery|ghar\s+(pe|par|mangwa\w*))\b", re.I)
SWITCH = re.compile(_W + r"(switch|change|make\s+it|instead|kar\s*do|kardo|karna|karein|kar\s+dein|chahiye|i'?ll"
                    r"|i\s+will|i\s+want|i'd\s+like|want|rather|rakh\w*|lunga|lungi|le\s+lunga|aa\s+ke|aakar"
                    r"|myself|khud)\b", re.I)
INSTEAD_OF = re.compile(r"\binstead\s+of\s+(\w[\w\s-]*)", re.I)


def order_type_call(message: str) -> Optional[Tuple[str, dict]]:
    """("set_order_type", {"order_type": ...}) when the customer switches delivery / pickup, else None. With both
    words, the target is the one not after "instead of", else the last one ("delivery se pickup kar do")."""
    text = PAYMENT_PHRASE.sub(" ", message)  # "cash on delivery" is a payment, not the order type
    hits = [(m.start(), "pickup" if m.group("pickup") else "delivery") for m in TYPE_WORD.finditer(text)]
    if not hits or not SWITCH.search(text):
        return None
    instead = INSTEAD_OF.search(text)
    if instead:
        hits = [h for h in hits if h[0] < instead.start()] or hits
    return "set_order_type", {"order_type": hits[-1][1]}


TRACK = re.compile(_W + r"(where\s+is\s+my\s+order|where'?s\s+my\s+order|(my\s+)?order\s+status|status\s+of\s+(my\s+)?order"
                   r"|track(ing)?\s+(my\s+)?order|(mera|meray|mere)\s+order\s+(kahan|kidhar|kab|ka\s+status)"
                   r"|order\s+(kahan|kidhar)\s+(hai|pohncha|pahuncha)|order\s+kab\s+(aye?ga|ayega|pohnchega)"
                   r"|kab\s+tak\s+(aa|aaye?|aye?|pohnch|pahunch)\w*|how\s+long\s+(will|till|until)\b.*\b(order|food|it)"
                   r"|میرا\s+آرڈر\s+کہاں|کب\s+تک\s+(آ|پہنچ))", re.I)
ORDER_NO = re.compile(r"\b\d{6}-\d{3,6}\b")


ORDER_NOW = re.compile(
    _W + r"(place\s+(the\s+|my\s+)?order|confirm(\s+(the|my))?\s+order|order\s+(confirm|place|kar\s*do|kardo|karo"
    r"|kar\s+dein|de\s+do|dedo|laga\s+do)|checkout|check\s+out|order\s+kar(na|ni)\s+hai|i'?m\s+ready\s+to\s+order)\b"
    r"|آرڈر\s+(کر\s*(دو|دیں|دیجیے|دیجئے)|کنفرم)", re.I)  # a command; "کیا آرڈر کریں؟" asks what to order


PRICE = re.compile(_W + r"(kitni|kitne|kitna|kitnay|kitny|price|prices|priced|cost|costs|rate|rates|how\s+much"
                   r"|qeemat|keemat)\b|قیمت|کتنے|کتنی|کتنا", re.I)
# Words around the item name in a price question; what's left is the search query.
QUESTION_WORDS = re.compile(
    _W + r"(how|much|what|whats|what's|is|are|the|a|an|of|for|your|you|do|does|have|price|prices|priced|cost|costs"
    r"|rate|rates|qeemat|keemat|kitni|kitne|kitna|kitnay|kitny|please|plz|pls|bhai|yaar|tell|me|i|want|to|know"
    r"|now|today|rs|in|at|this|branch|here)\b", re.I)
# A budget: "800 se kam", "1000 tak", "under 800", "less than Rs 1500", "budget 2000".
BUDGET = re.compile(
    r"(?P<a>\d{2,5})\s*(?:rs|rupay|rupees|rupe)?\s*(?:se|say|sy|sey)\s*(?:kam|kum|neeche|nichay|niche)"
    r"|(?P<b>\d{2,5})\s*(?:rs|rupay|rupees)?\s*(?:tak|takk|tk)\b"
    r"|\b(?:under|below|less\s+than|within|max(?:imum)?|upto|up\s+to|budget(?:\s+(?:of|is))?)\s*(?:rs\.?|pkr)?\s*(?P<c>\d{2,5})"
    r"|(?P<d>\d{2,5})\s*(?:or\s+less|max)\b", re.I)
BUDGET_FILLER = re.compile(
    _W + r"(kuch|kch|koi|se|say|sy|kam|kum|tak|under|below|less|than|within|max|maximum|upto|up|to|budget|of|is|rs|pkr"
    r"|rupay|rupees|chahiye|chaiye|hai|hain|ho|milega|milay|mile|dikhao|batao|bata|something|anything|i|want|for|me"
    r"|please|plz|the|a|an|in|my|mera|mere|liye|wala|wali|or|and|aur|ka|ki|ke|kya|what|can|get|have|you|do|any"
    r"|people|persons|person|log|logon|bande|afraad)\b", re.I)


def budget_call(message: str) -> Tuple[str, dict]:
    """search_menu with the budget as max_price and the rest of the words ("teekha") as the query."""
    m = BUDGET.search(message)
    limit = int(next(g for g in m.groups() if g))
    words = " ".join(re.findall(r"[^\W\d_]+", BUDGET_FILLER.sub(" ", message)))  # letters only, any script
    args = {"query": words or "popular", "max_price": limit}
    if DEALS.search(message):
        args["deals_only"] = True
    return "search_menu", args


# "Which burgers do you have?", "burger kon kon sa hain", "pizzas dikhao": list a kind of item.
MENU_ASK = re.compile(
    _W + r"(kon\s+kon|kaun\s+kaun|konsay|konse|konsi|kaunse|kaunsi|which|what\s+\w+(\s+\w+)?\s+do\s+you\s+have"
    r"|do\s+you\s+have|have\s+you\s+got|dikhao|dikha\s+do|show\s+me|list|options|varieties|types\s+of|kinds\s+of"
    r"|milte|milta|milti|available)\b|کون\s+کون|دکھائیں|دکھاؤ", re.I)
MENU_FILLER = re.compile(
    _W + r"(kon|kaun|konsay|konse|konsi|kaunse|kaunsi|which|show|list|options|option|varieties|types|kinds|got"
    r"|dikhao|dikha|milte|milta|milti|available|menu|sa|se|si|ki|ka|ke|hain|hai|wale|wali|wala|any|some|all)\b",
    re.I)
MAX_QUERY_WORDS = 4
MAX_PREFETCH = 2


def price_query(message: str) -> str:
    """'pizza kitni ki hai?' -> 'pizza'; '' when nothing item-like is left or too much is (then it isn't a
    plain price question and the model writes the search)."""
    text = MENU_FILLER.sub(" ", QUESTION_WORDS.sub(" ", ROMAN_URDU.sub(" ", message)))
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", text)
    if not 0 < len(words) <= MAX_QUERY_WORDS:
        return ""
    # Keep words the menu actually uses (or a kind like "burgers"): "which burger did you mention first" -> "burger"
    vocab = index().vocab
    menu_words = [w for w in words if w.lower() in vocab or w.lower() in cart.CATEGORY_WORDS]
    return " ".join(menu_words)


def certain(message: str) -> bool:
    """The word lists alone are sure what this is (place / confirm the order, track it, switch delivery /
    pickup): the planner call can be skipped, and prefetch_tools runs the same lookup."""
    return bool(ORDER_NOW.search(message) or TRACK.search(message) or order_type_call(message))


def prefetch_tools(message: str) -> List[Tuple[str, dict]]:
    """(tool, args) calls this message clearly needs, most specific first, at most MAX_PREFETCH."""
    calls: List[Tuple[str, dict]] = []
    if ORDER_NOW.search(message):  # "order confirm karo": check readiness; only the customer's "yes" places it
        return [("review_order", {})]
    switch = order_type_call(message)
    if switch:
        return [switch]
    if TRACK.search(message):  # a lookup only: works (and is answered from code) even when the model is down
        number = ORDER_NO.search(message)
        return [("get_order_status", {"order_no": number.group(0)} if number else {})]
    adding = ADD.search(message) or SIZE_ONLY.match(message) or (POINT.search(message) and not QUESTION.search(message))
    if adding and not NOT_ADD.search(message) and not SEVERAL.search(message) and not PAYMENT.search(message):
        return [add_call(message)]
    cart_view = CART_VIEW.search(message) and not CART_CHANGE.search(message)
    if cart_view:
        calls.append(("view_cart", {}))
    elif BUDGET.search(message) and not CART_CHANGE.search(message) and not PAYMENT.search(message):
        return [budget_call(message)]  # "kuch teekha 800 se kam", "deal under 1500"
    elif (PRICE.search(message) or MENU_ASK.search(message)) and not CART_CHANGE.search(message) \
            and not DEALS.search(message) and not PAYMENT.search(message):
        query = price_query(message)
        if query:
            calls.append(("search_menu", {"query": query}))
    if PAYMENT.search(message):
        if CHOICE.search(message):
            calls.append(("set_payment_method", {"method": message[:60]}))
        else:
            calls.append(("get_payment_methods", {}))
    if DEALS.search(message):
        calls.append(("get_deals", {}))
    if wants_recommendation(message) and not calls:  # "achi cheez" next to a price/cart question is a filler
        calls.append(("get_best_sellers", {}))
    if BRANCH.search(PAYMENT_PHRASE.sub(" ", message)):
        calls.append(("check_branch_status", {}))
    return calls[:MAX_PREFETCH]


# ---------- topics: which rule packs and tools this message needs ----------

ALL_TOPICS = ("menu", "cart", "payment", "checkout", "branch", "orders", "faq")
TOOL_TOPIC = {"search_menu": "menu", "get_item_details": "menu", "get_deals": "menu", "get_best_sellers": "menu",
              "calculate_total": "menu", "add_to_cart": "cart", "remove_from_cart": "cart", "view_cart": "cart",
              "clear_cart": "cart", "get_payment_methods": "payment", "set_payment_method": "payment",
              "set_quantity": "cart", "review_order": "checkout", "set_order_type": "checkout", "check_branch_status": "branch", "get_order_status": "orders",
              "get_order_history": "orders", "search_faq": "faq"}
TOPIC_TOOLS = {t: [n for n, tt in TOOL_TOPIC.items() if tt == t] for t in ALL_TOPICS}
ORDERS = re.compile(_W + r"(my\s+order|mera\s+order|meray\s+order|order\s+(status|kahan|kidhar|kab|history)"
                    r"|where\s+is\s+my|last\s+order|previous\s+order|purana\s+order|rider|track(ing)?)\b", re.I)
FAQ = re.compile(_W + r"(how\s+(do|can|to)\s+i|app\b|account|password|log\s*in|sign\s*up|register|notification"
                 r"|voucher|promo\s*code|wallet|refund|complaint|shikayat|support|helpline|contact)", re.I)


def topics(message: str, calls: List[Tuple[str, dict]], offered: bool) -> List[str]:
    """Topics of this message, from the lookups the code already chose plus a few cues. Menu is always in (items
    and prices come up everywhere). Nothing recognised -> every topic: the full rules and tools, never fewer."""
    found = {TOOL_TOPIC[name] for name, _ in calls if name in TOOL_TOPIC}
    if CART_VIEW.search(message) or CART_CHANGE.search(message) or ADD.search(message) or offered:
        found.add("cart")
    if PAYMENT.search(message):
        found |= {"payment", "cart"}
    if ORDER_NOW.search(message) or order_type_call(message):
        found |= {"checkout", "cart", "payment"}
    if BRANCH.search(PAYMENT_PHRASE.sub(" ", message)):
        found.add("branch")
    if ORDERS.search(message):
        found.add("orders")
    if FAQ.search(message):
        found.add("faq")
    if not found:
        return list(ALL_TOPICS)
    found.add("menu")
    return [t for t in ALL_TOPICS if t in found]


def tools_for(topic_list: List[str]) -> List[str]:
    return [n for t in topic_list for n in TOPIC_TOOLS[t]]
