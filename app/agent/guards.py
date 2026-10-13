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


def check_answer(answer: str, tool_results: Iterable[dict], tools_called: Iterable[str], user_message: str) -> List[str]:
    """Return a list of problems; empty means the answer passes."""
    results = list(tool_results)
    called = set(tools_called)
    problems = []
    amounts = amounts_in(answer)
    if amounts:
        if not results:
            problems.append("states a price but no tool was called this turn")
        else:
            allowed = numbers_in(results) | {float(n) for n in NUMBER.findall(user_message.replace(",", ""))}
            bad = [a for a in amounts if a not in allowed]
            if bad:
                problems.append(f"amount(s) {sorted(set(bad))} not found in tool results")
    if STATUS_WORDS.search(answer or "") and not (called & ORDER_TOOLS) and "order" in (answer or "").lower():
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
    r"kaunsi|kis|kya|wo|woh|ye|yeh|ka|ki|ke|sa|si|se|say|ko|mein|mai|men|raha|rahi|tha|thi|wahan|yahan)\b",
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
    ROMAN_URDU_LANG: "The customer wrote in Roman Urdu (Urdu in English letters): reply in Roman Urdu.",
    MIXED: "The customer mixed Roman Urdu and English: reply in the same mix.",
    LATIN: ("Reply in exactly the language of the customer's latest message. If it is Roman Urdu "
            "(Urdu written in English letters, e.g. 'sab se zyada kon sa hai'), reply in Roman Urdu; "
            "if it is English, reply in English."),
}
