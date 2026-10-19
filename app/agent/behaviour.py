"""Behaviour layer: what kind of message is this, before any tool or model runs.

Greetings, thanks / goodbyes, "who are you", insults and off-topic chat get a fixed reply in the customer's
language, with no tools: a small model otherwise answers "hi" or an insult with a menu search. Anything that
looks like food, the cart, payment, branches, orders or the app goes to the agent unchanged, and so does a
short reply in the middle of a conversation ("haan", "2", "pehli wali"): that answers the bot's last question.

Word lists catch the clear cases. A message with no business cue at all gets one cheap model call (no tools)
that only says FOOD or OTHER; when in doubt, or if that call fails, it's FOOD (the normal path).
"""
import re
from functools import lru_cache
from typing import Callable, Optional

from app.agent import intents
from app.agent.guards import MIXED, ROMAN_URDU, ROMAN_URDU_LANG, URDU_SCRIPT
from app.retrieval import _synonyms, index

GREETING, THANKS, BYE, IDENTITY, ABUSE, OFF_TOPIC = "greeting", "thanks", "bye", "identity", "abuse", "off_topic"


def _norm(text: str) -> str:
    """Lowercase, runs of one letter squeezed: "kuttay" -> "kutay", "chroooo" -> "chro", "heyyy" -> "hey"."""
    return re.sub(r"(.)\1+", r"\1", text.lower())


def _words(text: str) -> list:
    return re.findall(r"[^\W\d_]+", _norm(text))


def _wordset(words: str) -> frozenset:
    return frozenset(_norm(w) for w in words.split())


# Insults and profanity: English, Roman Urdu (common spellings) and Urdu script. Matched as whole words after
# _norm, so spelling stretches ("kuttayyy") still match.
ENGLISH_ABUSE = """fuck fucking fucker fuk fck motherfucker shit bullshit bitch bastard asshole arsehole dick cunt
slut whore idiot stupid dumb moron retard loser wtf stfu bc mc"""
ABUSE_WORDS = _wordset(ENGLISH_ABUSE + """
kutta kutte kuttay kuttey kuttey kutti kuttiya kutiya kamina kamine kaminay kamini harami haramzada haramzaday
haramzadi haramkhor khabees ghatiya chutiya chutiye chutya chootiya chuttiya bhosda bhosdi bhosri bhosdike
bhosdiwale gandu gaandu gaand gand lun lund lul randi rundi behenchod bhenchod benchod bhen madarchod
maderchod madarjaat chod choot phudi phuddi phudu phuda suar suwar sooar khinzir ullu gadha gadhay gadhe
bewakoof bewaqoof bewkoof pagal jahil badtameez beghairat tharki
کتا کتے کتی کتیا حرامی حرامزادہ حرامزادے کمینہ کمینے گدھا گدھے الو بیوقوف چوتیا لن گانڈ سور بے_غیرت
""".replace("_", " "))
ABUSE_PHRASES = re.compile(r"\b(shut\s+up|go\s+to\s+hell|f\s*off|ullu\s+ka\s+pat+ha|bhen\s*ch\w*|maa\s+ch\w*"
                           r"|teri\s+maa\w*|tery\s+maa\w*|teri\s+behn\w*)\b|بہن\s*چود|ماں\s*چود|مادر\s*چود", re.I)

GREETING_WORDS = _wordset("hi hii hello helo hey hy salam salaam slam aoa assalam asalam assalamualaikum "
                          "assalamoalaikum aslam salamz السلام سلام ہیلو")
GREETING_FILLER = _wordset("o u a alaikum alaikom alikum alekum aleikum walaikum wa good morning evening afternoon "
                           "night day there all everyone bhai sir yaar yar dear ji jee g team cheezious "
                           "علیکم ہیلو جی بھائی")
THANKS_WORDS = _wordset("thanks thank thx ty tysm shukriya shukria shukriyah shukrya jazakallah jazakallahu "
                        "jazak meherbani mehrbani شکریہ جزاک")
BYE_WORDS = _wordset("bye goodbye byebye khuda hafiz allah hafiz خدا حافظ اللہ")
THANKS_FILLER = _wordset("you so much very a lot lots ok okay okk acha achha theek thik hai bhai yaar sir ji "
                         "jee g allah khair take care for help the your mam madam boht bohat bahut")
IDENTITY_ASK = re.compile(
    r"\b(who\s+(are|r)\s+(you|u)|what\s+(are|r)\s+(you|u)|(are|r)\s+(you|u)\s+(a\s+)?(bot|robot|human|real|ai|person|"
    r"machine|chatgpt)|your\s+name|(tum|tm|ap|aap)\s+(kaun|kon|kon\s+ho|kaun\s+ho)|(tumhara|tmhara|apka|aapka)\s+naam"
    r"|(tum|ap|aap)\s+(bot|robot|insaan|insan|machine)\s+(ho|hain|hai)|kya\s+(tum|ap|aap)\s+(insaan|insan|bot|robot))\b"
    r"|آپ\s+کون|تم\s+کون|آپ\s+کا\s+نام", re.I)

# Menu words that count as "about food". Short words and Urdu filler are left out: the category
# "Bhook Ka The End!" would otherwise make "ka" and "the" food words.
NOT_FOOD = {"the", "end", "back", "and", "with", "for", "i'm", "only", "valid", "app", "somewhat", "amazing",
            "extremely", "good", "treat", "treats", "love", "local", "special", "lover", "royal", "zone", "new"}


@lru_cache(maxsize=1)
def _food_words() -> frozenset:
    words = {w for w in index().vocab if len(w) >= 3 and not ROMAN_URDU.fullmatch(w)} - NOT_FOOD
    for key, values in _synonyms().items():
        words |= {w for w in re.findall(r"[^\W\d_]+", f"{key} {' '.join(values)}".lower()) if len(w) >= 3}
    return frozenset(_norm(w) for w in words - NOT_FOOD)


BUSINESS_CUES = (intents.CART_VIEW, intents.CART_CHANGE, intents.ADD, intents.BRANCH, intents.DEALS, intents.BEST,
                 intents.PAYMENT, intents.ORDER_NOW, intents.ORDERS, intents.FAQ)
# Only about food next to a food word or after the bot showed something: "2+2 kitne hote hain" is not a price
# question, "pizza kitne ki hai" and (after a list) "kitne ki hai?" are.
WEAK_CUES = (intents.PRICE, intents.MENU_ASK, intents.BUDGET, intents.QUALITY)
FOOD_TALK = re.compile(r"\b(khana|khanay|khane|khaana|bhook|bhooka|bhooki|bhuk|hungry|eat|food|order|menu|taste|"
                       r"flavou?r|teekha|tikha|meetha|metha|khatta|namkeen|spicy|sweet|sour|cheesy|crispy|delivery|"
                       r"pizza|burger|deal|dil\s+kar)\w*", re.I)


def business(message: str) -> bool:
    """Does the message mention anything the agent handles (food, cart, payment, branch, orders, the app)?"""
    if any(p.search(message) for p in BUSINESS_CUES) or FOOD_TALK.search(message) \
            or intents.wants_recommendation(message):
        return True
    return any(w in _food_words() for w in _words(message))


def weak_cue(message: str) -> bool:
    return any(p.search(message) for p in WEAK_CUES)


def _only(words: list, core: frozenset, filler: frozenset) -> bool:
    return bool(words) and any(w in core for w in words) and all(w in core or w in filler for w in words)


def fast_kind(message: str) -> Optional[str]:
    """The kind of message the word lists are sure about, or None."""
    words = _words(message)
    food = business(message)
    if not food and (any(w in ABUSE_WORDS for w in words) or ABUSE_PHRASES.search(message)):
        return ABUSE
    if _only(words, BYE_WORDS, THANKS_WORDS | THANKS_FILLER):
        return BYE
    if _only(words, THANKS_WORDS, THANKS_FILLER):
        return THANKS
    if _only(words, GREETING_WORDS, GREETING_FILLER):
        return GREETING
    if not food and IDENTITY_ASK.search(message):
        return IDENTITY
    return None


ROUTER_PROMPT = (
    "You sort customer messages for the Cheezious fast-food app assistant (pizza, burgers, deals, branches, "
    "ordering, cart, payment, delivery, the customer's orders, the app). Answer with ONE word.\n"
    "FOOD: anything about food, cravings, tastes, hunger, the menu, prices, deals, ordering, the cart, payment, "
    "delivery, branches, their orders or the app, in any language. Also FOOD when unsure.\n"
    "OTHER: clearly something else (weather, news, sports, maths, coding, homework, jokes, politics, "
    "relationships, other companies).\n"
    "Message: {message}\nAnswer:")


def kind(message: str, in_conversation: bool, ask_model: Callable[[str], str]) -> Optional[str]:
    """None = normal agent path. in_conversation: earlier turns exist, so a short message may be a reply."""
    found = fast_kind(message)
    if found:
        return found
    if business(message) or (in_conversation and (len(_words(message)) <= 3 or weak_cue(message))):
        return None
    try:
        label = (ask_model(ROUTER_PROMPT.format(message=message[:300])) or "").strip().upper()
    except Exception:  # noqa: BLE001 - router down: the normal path handles it
        return None
    return OFF_TOPIC if label.startswith("OTHER") else None


# Roman Urdu words in these short messages that the language detector doesn't know: reply in Roman Urdu.
ROMAN_WORDS = (ABUSE_WORDS - _wordset(ENGLISH_ABUSE)) | _wordset(
    "shukriya shukria shukriyah shukrya jazakallah jazak meherbani mehrbani khuda hafiz")
SALAM = re.compile(r"\b(salam|salaam|slam|aoa|assalam\w*|asalam\w*|aslam)\b|السلام|سلام", re.I)

REPLIES = {
    GREETING: {
        "latin": "Hi! Welcome to Cheezious. I can show you the menu and deals, check prices, build your cart or "
                 "track your order. What would you like today?",
        ROMAN_URDU_LANG: "{salam} Cheezious mein khush aamdeed. Main menu, deals, prices, cart aur aap ke order "
                         "mein madad kar sakta hoon. Aaj kya khana pasand karenge?",
        URDU_SCRIPT: "{salam} چیزیئس میں خوش آمدید۔ میں مینو، ڈیلز، قیمتوں، کارٹ اور آپ کے آرڈر میں مدد کر سکتا ہوں۔ "
                     "آج کیا کھانا پسند کریں گے؟",
    },
    THANKS: {
        "latin": "You're welcome! Is there anything else I can help you with?",
        ROMAN_URDU_LANG: "Koi baat nahi! Kisi aur cheez mein madad chahiye?",
        URDU_SCRIPT: "کوئی بات نہیں! کسی اور چیز میں مدد چاہیے؟",
    },
    BYE: {
        "latin": "Thanks for choosing Cheezious. Allah Hafiz!",
        ROMAN_URDU_LANG: "Cheezious chunne ka shukriya. Allah Hafiz!",
        URDU_SCRIPT: "چیزیئس کا انتخاب کرنے کا شکریہ۔ اللہ حافظ!",
    },
    IDENTITY: {
        "latin": "I'm Cheezious Assistant, a virtual assistant (not a person). I can help with the menu, deals, "
                 "prices, your cart, payment options, branch timings and your orders.",
        ROMAN_URDU_LANG: "Main Cheezious Assistant hoon, ek virtual assistant (insaan nahi). Menu, deals, prices, "
                         "cart, payment, branch timings aur aap ke orders mein madad kar sakta hoon.",
        URDU_SCRIPT: "میں چیزیئس اسسٹنٹ ہوں، ایک ورچوئل اسسٹنٹ (انسان نہیں)۔ مینو، ڈیلز، قیمتیں، کارٹ، ادائیگی، "
                     "برانچ کے اوقات اور آپ کے آرڈرز میں مدد کر سکتا ہوں۔",
    },
    ABUSE: {
        "latin": "I'm here to help with your Cheezious order, so let's keep it respectful. Would you like to see "
                 "the menu or today's deals?",
        ROMAN_URDU_LANG: "Main aap ki madad ke liye haazir hoon, baraye meharbani tameez se baat karein. Menu ya "
                         "deals dikhaun?",
        URDU_SCRIPT: "میں آپ کی مدد کے لیے حاضر ہوں، براہ کرم تمیز سے بات کریں۔ مینو یا ڈیلز دکھاؤں؟",
    },
    OFF_TOPIC: {
        "latin": "Sorry, I can only help with Cheezious: the menu, deals, prices, your cart, branches and your "
                 "orders. What would you like to order?",
        ROMAN_URDU_LANG: "Maaf kijiye, main sirf Cheezious ke menu, deals, prices, cart, branches aur orders mein "
                         "madad kar sakta hoon. Kya order karna chahenge?",
        URDU_SCRIPT: "معذرت، میں صرف چیزیئس کے مینو، ڈیلز، قیمتوں، کارٹ، برانچز اور آرڈرز میں مدد کر سکتا ہوں۔ "
                     "کیا آرڈر کرنا چاہیں گے؟",
    },
}


def reply(kind_: str, language: str, message: str) -> str:
    salam = bool(SALAM.search(message))
    roman = language in (ROMAN_URDU_LANG, MIXED) or salam or any(w in ROMAN_WORDS for w in _words(message))
    lang = URDU_SCRIPT if language == URDU_SCRIPT else ROMAN_URDU_LANG if roman else "latin"
    text = REPLIES[kind_][lang]
    hello = ("Walaikum Assalam!" if salam else "Assalam o Alaikum!") if lang == ROMAN_URDU_LANG else \
        ("وعلیکم السلام!" if salam else "السلام علیکم!")
    return text.replace("{salam}", hello)
