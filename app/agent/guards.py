"""Post-checks that make answers trustworthy (spec 10): price guard, grounding guard, PII redaction."""
import json
import re
import unicodedata
from typing import Iterable, List, Optional, Set

AMOUNT = re.compile(
    r"(?:\b(?:rs\.?|pkr|rupees?)|₨|روپے)\s*([\d][\d,]*(?:\.\d+)?)"
    r"|([\d][\d,]*(?:\.\d+)?)\s*(?:/-|(?:rs\.?|pkr|rupees?|روپے)\b)",
    re.I,
)
NUMBER = re.compile(r"\d+(?:\.\d+)?")
STATUS_WORDS = re.compile(
    r"\b(delivered|preparing|picked up|ready at (the )?kitchen|out for delivery|on the way|cancell?ed|"
    r"ready for pick ?up|waiting for (a )?rider)\b", re.I)
ORDER_TOOLS = {"get_order_status", "get_order_history"}
# Payment answers say "cash on delivery is available" / "pay when the order is delivered": that's the payment
# method, not the branch's delivery service or an order's status.
PAYMENT_TOOLS = {"get_payment_methods", "set_payment_method", "review_order"}
PAYMENT_METHOD_PHRASE = re.compile(r"\b(?:cash|card|pay\w*|qr)\s+on\s+(?:delivery|pick-?up)\b", re.I)
# Claims about a branch being open, its hours or its services: only check_branch_status can back them.
BRANCH_CLAIM = re.compile(
    r"\b(?:is|are) (?:currently |now |still )?(?:open|closed)\b|\b(?:open|closed) (?:right )?now\b|"
    r"\bopening hours\b|\btimings?\b|\b(?:open|closes?|closing) (?:at|until|till|from)\b|"
    r"\b(?:delivery|pick-?up|dine[- ]?in|takeaway)\b[^.]{0,30}\b(?:is |are )?(?:available|offered)\b|"
    r"\bkhul(?:i|a|ti|ta) (?:hai|hui|hoti)\b|\bband (?:hai|hoti|hogi)\b", re.I)
URDU_CHAR = re.compile(r"[\u0600-\u06FF]")
LETTER = re.compile(r"[^\W\d_]")
# Phrases that admit an inexact match (English, Roman Urdu, Urdu).
UNSURE = re.compile(r"did you mean|do you mean|couldn['’]?t find|could not find|can['’]?t find|not find|"
                    r"exactly|closest|not sure|spelling|read .{1,30} as|matlab|shayad|kya aap .{0,20}(chahte|keh)|"
                    r"مطلب|شاید|نہیں ملا", re.I)
LOOKUP_TOOLS = {"search_menu", "get_item_details", "get_deals", "get_best_sellers", "calculate_total",
                "add_to_cart", "remove_from_cart", "view_cart", "clear_cart", "get_payment_methods",
                "set_payment_method", "review_order"}
PHONE = re.compile(r"(?:\+92|0092|\b0)3\d{2}[\s-]?\d{7}\b")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


DIGIT_GROUP_SPACE = re.compile(r"(?<=\d)[\s\u00a0\u202f\u2009](?=\d{3}\b)")


def amounts_in(text: str) -> List[float]:
    out = []
    text = DIGIT_GROUP_SPACE.sub("", text or "")  # "Rs 1 350" -> "Rs 1350"
    for m in AMOUNT.finditer(text):
        raw = (m.group(1) or m.group(2)).replace(",", "")
        out.append(float(raw))
    return out


def numbers_in(obj) -> Set[float]:
    """Every number appearing anywhere in tool results (values and digits inside strings)."""
    found: Set[float] = set()

    def walk(x):
        if isinstance(x, bool):
            return
        if isinstance(x, (int, float)):
            found.add(float(x))
        elif isinstance(x, str):
            found.update(float(n) for n in NUMBER.findall(x.replace(",", "")))
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return found


WRONG_SCRIPT = ("replied in Urdu script to a message written in English letters: write the same answer again "
                "in Roman Urdu, using English letters only")
NOT_ROMAN_URDU = "the customer wrote in Roman Urdu: reply in Roman Urdu (Urdu in English letters), not English"


def check_answer(answer: str, tool_results: Iterable[dict], tools_called: Iterable[str], user_message: str,
                 menu_names: Iterable[str] = ()) -> List[str]:
    """Return a list of problems; empty means the answer passes."""
    results = list(tool_results)
    called = set(tools_called)
    problems = []
    text = answer or ""
    for r in results:
        if isinstance(r, dict) and r.get("match") == "several":
            names = {re.sub(r"[^0-9a-z]+", " ", (i.get("name") or "").lower()).strip() for i in r.get("items") or []}
            low = f" {re.sub(r'[^0-9a-z]+', ' ', text.lower())} "
            if len(names) > 1 and sum(f" {n} " in low for n in names if n) < 2:
                problems.append("several different items match: list them with type and starting price and ask "
                                "which one the customer means; do not pick one")
    if any(r.get("exact") is False for r in results if isinstance(r, dict)) and not UNSURE.search(text):
        problems.append("the menu has nothing named exactly what the customer wrote: say you couldn't find it "
                        "exactly and ask 'did you mean ...?' instead of answering as if it were certain")
    letters = LETTER.findall(text)
    if letters and not URDU_CHAR.search(user_message or "") and len(URDU_CHAR.findall(text)) / len(letters) > 0.3:
        problems.append(WRONG_SCRIPT)
    if detect_language(user_message or "") == ROMAN_URDU_LANG and len(text.split()) > 8 \
            and len(ROMAN_URDU.findall(text)) < 3 and not URDU_CHAR.search(text):
        problems.append(NOT_ROMAN_URDU)
    if BRANCH_CLAIM.search(PAYMENT_METHOD_PHRASE.sub(" ", text)) and "check_branch_status" not in called:
        problems.append("states branch status, hours or services without calling check_branch_status")
    if not called & LOOKUP_TOOLS:
        low = f" {re.sub(r'[^0-9a-z]+', ' ', text.lower())} "
        named = [n for n in menu_names if len(n) > 3 and f" {n} " in low]
        if named:
            problems.append(f"describes menu item(s) {named[:3]} without looking them up this turn")
    amounts = amounts_in(answer)
    if amounts:
        if not results:
            problems.append("states a price but no tool was called this turn")
        else:
            allowed = numbers_in(results) | {float(n) for n in NUMBER.findall(user_message.replace(",", ""))}
            bad = [a for a in amounts if a not in allowed]
            if bad:
                problems.append(f"amount(s) {sorted(set(bad))} not found in tool results")
    if STATUS_WORDS.search(answer or "") and not (called & (ORDER_TOOLS | PAYMENT_TOOLS)) \
            and "order" in (answer or "").lower():
        problems.append("states an order status without calling an order tool")
    return problems


def redact(text: str) -> str:
    """PII redaction for logs/traces."""
    return EMAIL.sub("<email>", PHONE.sub("<phone>", text or ""))


def redact_obj(obj):
    return json.loads(redact(json.dumps(obj, ensure_ascii=False, default=str)))


# ---------- pre-checks ----------

MAX_MESSAGE_CHARS = 1000
# Roman Urdu words (several spellings) that are not also English words, so "main", "do", "or" are left out.
# This list only strengthens the hint; when it finds nothing the model is told to mirror the customer.
ROMAN_URDU = re.compile(
    r"\b(hai|hain|hy|ha|hen|kya|kia|kyun|kyon|mera|meri|mere|mujhe|mujhay|muje|hum|humein|aap|ap|apka|"
    r"tum|kitna|kitne|kitni|kitnay|kitny|chahiye|chahye|chaiye|aur|wala|wali|walay|wale|kab|kahan|kidhar|"
    r"kaise|kese|kesa|kuch|kch|koi|nahi|nahin|nai|hoga|hogi|karo|kar|kr|krna|krdo|bata|batao|btao|"
    r"bta|dena|dedo|bhi|abhi|jaldi|bhai|yaar|yar|acha|achha|theek|thik|zyada|zayada|zayda|ziada|ziyada|"
    r"kam|sasta|mehnga|teekha|meetha|thanda|garam|khana|sab|sabse|sabsay|kon|kaun|konsa|kaunsa|konsi|"
    r"kaunsi|kis|kya|wo|woh|ye|yeh|ka|ki|ke|sa|si|se|say|ko|mein|mai|men|raha|rahi|tha|thi|wahan|yahan|"
    # Not here on purpose: words that are also common English (to, do, main, par, hum is kept above).
    r"kiya|ma|mn|apna|apni|apne|apny|hu|hoon|hun|karun|karoon|krun|chahta|chahti|chahte|sakta|sakti|sakte|"
    r"khilao|khilaon|khilaun|khila|khilana|dikhao|dikha|dikhado|lao|lena|lene|lelo|dijiye|dijye|kijiye|"
    r"bohat|bahut|bohot|boht|zara|liye|liya|pe|milega|milegi|chalega|chalegi|wala|tak|sirf|lekin|magar)\b",
    re.I)


def pre_check(message: str) -> Optional[str]:
    """Return a cleaned message, or None if it must be rejected."""
    message = "".join(ch for ch in message if unicodedata.category(ch)[0] != "C" or ch in "\n\t").strip()
    if not message or len(message) > MAX_MESSAGE_CHARS:
        return None
    return message


URDU_SCRIPT, ROMAN_URDU_LANG, MIXED, LATIN = "urdu_script", "roman_urdu", "mixed", "latin"


def detect_language(message: str) -> str:
    """Rough language/script class of a message. LATIN means 'not sure': English or Roman Urdu."""
    letters = [ch for ch in message if ch.isalpha()]
    if letters and sum("\u0600" <= ch <= "\u06ff" for ch in letters) / len(letters) > 0.3:
        return URDU_SCRIPT
    words = re.findall(r"[A-Za-z]+", message)
    urdu_words = len(ROMAN_URDU.findall(message))
    if not urdu_words:
        return LATIN
    return ROMAN_URDU_LANG if urdu_words / max(len(words), 1) >= 0.4 else MIXED


LANGUAGE_HINTS = {
    URDU_SCRIPT: "The customer wrote in Urdu script: reply in Urdu script.",
    ROMAN_URDU_LANG: ("The customer wrote in Roman Urdu (Urdu in English letters): reply in Roman Urdu, "
                      "e.g. \"Humare paas teen burgers hain, kaunsa add karun?\" Use English letters only, never "
                      "Urdu script, and not English."),
    MIXED: "The customer mixed Roman Urdu and English: reply in the same mix.",
    LATIN: ("The customer wrote in English letters with no Urdu words we recognise: reply in English, "
            "unless the message is clearly Roman Urdu (then reply in Roman Urdu). Never reply in Urdu script."),
}

# Repeated at the end of the customer's message in the prompt (not in memory): models, small ones most,
# follow the last thing they read; Ollama moves every system message to the top of the prompt.
REPLY_NOTES = {
    URDU_SCRIPT: "(جواب اردو رسم الخط میں دیں۔)",
    # No word "Urdu" here: small models read it as "write Urdu script". The example is safe to copy anywhere.
    ROMAN_URDU_LANG: "(Jawab isi tarah English letters mein likhein, jaise: \"Aap ke liye yeh options hain.\")",
    MIXED: "(Reply in the same mix of Roman Urdu and English.)",
}
