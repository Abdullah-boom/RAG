"""The order summary, the customer's yes / no to it, and what the bot says after placing: all fixed text from
code, never the model's words, because this is where money and a real order are involved.

Flow: review_order is ready -> the bot shows summary_text() with Confirm / Cancel buttons (the API keeps a pending
confirm token for that chat session) -> the customer taps Confirm, or types it: answer_to_confirm() reads a plain
yes / no; yes places the order (app/api.py), no drops it, anything else drops the pending token and is answered as
a normal message. Not ready: not_ready_text() says what's missing, with the payment choices when that's it.
"""
import re
from typing import Optional

from app import payments

LANGS = ("latin", "roman_urdu", "urdu_script")

YES = re.compile(
    r"^\s*(yes|yess|yeah|yep|yup|y|ok|okay|okk|sure|confirm|confirmed|place\s+it|place\s+(the\s+|my\s+)?order"
    r"|go\s+ahead|do\s+it|haan|han|haa|hn|ji|jee|ji\s+haan|g|theek\s+hai|thik\s+hai|kar\s+do|kardo|kar\s+dein"
    r"|order\s+kar\s+do|confirm\s+kar\s+do|ہاں|جی|جی\s+ہاں|ٹھیک\s+ہے)"
    r"(\s+(please|plz|pls|ji|jee|kar\s+do|kardo|confirm|it|order|place\s+it|go\s+ahead))*\s*[.!]*\s*$", re.I)
_NO_WORD = (r"(no|nope|nah|n|cancel|stop|wait|not\s+now|don'?t|nahi|nahin|nai|na|mat\s+karo|ruko|ruk\s+jao|ruk|"
            r"abhi\s+nahi|نہیں|رکو)")
NO = re.compile(  # one or more no-words: "nahi ruko", "no wait", "nahi abhi nahi"
    rf"^\s*{_NO_WORD}(\s*[,.]?\s+({_NO_WORD}|please|plz|ji|thanks|abhi|yet|now|bhai))*\s*[.!]*\s*$", re.I)


# One-word answers are too short for language detection: "haan" / "nahi" are Roman Urdu.
ROMAN_ANSWER = re.compile(r"\b(haan|han|haa|hn|ji|jee|theek|thik|kar\s*do|kardo|kar\s+dein|nahi|nahin|nai|mat|ruko|ruk"
                          r"|abhi)\b", re.I)


def answer_language(message: str, detected: str) -> str:
    if re.search(r"[\u0600-\u06FF]", message or ""):
        return "urdu_script"
    return "roman_urdu" if ROMAN_ANSWER.search(message or "") else detected


def answer_to_confirm(message: str) -> Optional[str]:
    """"yes", "no", or None when the message is something else (a change, a question)."""
    if YES.match(message or ""):
        return "yes"
    if NO.match(message or ""):
        return "no"
    return None


def rs(n) -> str:
    return f"Rs {int(n):,}"


_T = {
    "head": {"latin": "Here's your order:", "roman_urdu": "Yeh raha aap ka order:", "urdu_script": "آپ کا آرڈر:"},
    "DELIVERY": {"latin": "Delivery from {branch}", "roman_urdu": "{branch} se delivery",
                 "urdu_script": "{branch} سے ڈیلیوری"},
    "PICKUP": {"latin": "Pickup from {branch}", "roman_urdu": "{branch} se pickup", "urdu_script": "{branch} سے پک اپ"},
    "pay": {"latin": "Payment: {name}", "roman_urdu": "Payment: {name}", "urdu_script": "ادائیگی: {name}"},
    "total": {"latin": "Estimated total: {total}{tax}", "roman_urdu": "Andaazan total: {total}{tax}",
              "urdu_script": "اندازاً کل: {total}{tax}"},
    "tax": {"latin": " (incl. {rate}% tax)", "roman_urdu": " ({rate}% tax samait)", "urdu_script": " ({rate}% ٹیکس سمیت)"},
    "note": {"latin": "Delivery fee, discounts and vouchers are added at checkout.",
             "roman_urdu": "Delivery fee, discounts aur vouchers checkout par lagte hain.",
             "urdu_script": "ڈیلیوری فیس، رعایت اور واؤچر چیک آؤٹ پر شامل ہوں گے۔"},
    "note_pickup": {"latin": "Discounts and vouchers are added at checkout.",
                    "roman_urdu": "Discounts aur vouchers checkout par lagte hain.",
                    "urdu_script": "رعایت اور واؤچر چیک آؤٹ پر شامل ہوں گے۔"},
    "ask": {"latin": "Tap Confirm order (or reply \"yes\") to place it, or tell me what to change.",
            "roman_urdu": "Order place karne ke liye Confirm order dabayein (ya \"haan\" likhein), ya batayein kya "
                          "badalna hai.",
            "urdu_script": "آرڈر کرنے کے لیے Confirm order دبائیں (یا \"ہاں\" لکھیں)، یا بتائیں کیا بدلنا ہے۔"},
    "pay_now": {"latin": "You'll pay online in the app's secure checkout.",
                "roman_urdu": "Online payment app ke secure checkout mein hogi.",
                "urdu_script": "آن لائن ادائیگی ایپ کے محفوظ چیک آؤٹ میں ہوگی۔"},
}


def _t(key: str, lang: str) -> str:
    return _T[key].get(lang) or _T[key]["latin"]


def summary_text(review: dict, lang: str) -> str:
    """The ready order, from review_order's summary, ending with the "reply yes" question."""
    s = review["summary"]
    lines = [f"- {l['qty']} × {l['name']}" + (f" ({l['size']})" if l.get("size") else "")
             + (f" – {rs(l['line_total'])}" if l.get("line_total") is not None else "") for l in s["lines"]]
    m = s.get("payment_method") or {}
    total = m.get("total") if m.get("total") is not None else s["subtotal"]
    tax = _t("tax", lang).format(rate=m["tax_rate"]) if m.get("tax_rate") is not None else ""
    details = [_t(s["order_type"], lang).format(branch=s.get("branch") or "your branch"),
               _t("pay", lang).format(name=m.get("name", "")),
               _t("total", lang).format(total=rs(total), tax=tax)]
    notes = _t("note_pickup" if s["order_type"] == payments.PICKUP else "note", lang)
    if m.get("pay_now"):
        notes += " " + _t("pay_now", lang)
    # Markdown (the widget renders it): a blank line around the list, "  \n" keeps the detail lines apart.
    return "\n\n".join([_t("head", lang), "\n".join(lines), "  \n".join(details), notes, _t("ask", lang)])


_DONE = {
    "placed": {"latin": "Your order is placed! {how} I'll keep track of it: just ask me \"where is my order?\" "
                        "any time.",
               "roman_urdu": "Aap ka order place ho gaya hai! {how} Main iska khayal rakhunga: jab chahein poochein "
                             "\"mera order kahan hai?\"",
               "urdu_script": "آپ کا آرڈر ہو گیا ہے! {how} میں اس پر نظر رکھوں گا: جب چاہیں پوچھیں \"میرا آرڈر کہاں ہے؟\""},
    "DELIVERY": {"latin": "It will be delivered to you.", "roman_urdu": "Yeh aap tak deliver hoga.",
                 "urdu_script": "یہ آپ تک پہنچا دیا جائے گا۔"},
    "PICKUP": {"latin": "We'll let you know when it's ready for pickup.",
               "roman_urdu": "Tayyar hone par pickup ke liye bata diya jayega.",
               "urdu_script": "تیار ہونے پر پک اپ کے لیے بتا دیا جائے گا۔"},
    "pay_now": {"latin": "Please complete the online payment in the app.",
                "roman_urdu": "Meharbani kar ke app mein online payment mukammal karein.",
                "urdu_script": "براہ کرم ایپ میں آن لائن ادائیگی مکمل کریں۔"},
    "not_connected": {"latin": "Your cart, order type and payment choice are saved. Ordering from chat isn't "
                               "switched on yet, so please complete the order in the app.",
                      "roman_urdu": "Aap ka cart, order type aur payment saved hain. Chat se order abhi on nahi hai, "
                                    "is liye app mein order mukammal karein.",
                      "urdu_script": "آپ کا کارٹ، آرڈر کی قسم اور ادائیگی محفوظ ہیں۔ چیٹ سے آرڈر ابھی دستیاب نہیں، "
                                     "اس لیے ایپ میں آرڈر مکمل کریں۔"},
    "rejected": {"latin": "Sorry, the order couldn't be placed{why}. Your cart is saved, so you can change it and "
                          "try again.",
                 "roman_urdu": "Maaf kijiye, order place nahi ho saka{why}. Aap ka cart saved hai, badal kar dobara "
                               "try karein.",
                 "urdu_script": "معذرت، آرڈر نہیں ہو سکا{why}۔ آپ کا کارٹ محفوظ ہے، بدل کر دوبارہ کوشش کریں۔"},
    "failed": {"latin": "I couldn't reach our ordering system. Your cart is saved: reply \"yes\" in a moment to try "
                        "again.",
               "roman_urdu": "Ordering system se rabta nahi ho saka. Aap ka cart saved hai: thori der mein \"haan\" "
                             "likh kar dobara try karein.",
               "urdu_script": "آرڈرنگ سسٹم سے رابطہ نہیں ہو سکا۔ آپ کا کارٹ محفوظ ہے: کچھ دیر میں \"ہاں\" لکھ کر دوبارہ "
                              "کوشش کریں۔"},
    "changed": {"latin": "Your order changed since I showed it, so I haven't placed it. Here it is again:",
                "roman_urdu": "Dikhane ke baad aap ka order badal gaya, is liye place nahi kiya. Dobara dekh lein:",
                "urdu_script": "دکھانے کے بعد آرڈر بدل گیا، اس لیے نہیں کیا۔ دوبارہ دیکھ لیں:"},
    "declined": {"latin": "Okay, I haven't placed it. Your cart is saved; tell me when you're ready or what to "
                          "change.",
                 "roman_urdu": "Theek hai, order place nahi kiya. Aap ka cart saved hai; jab tayyar hon ya kuch "
                               "badalna ho to batayein.",
                 "urdu_script": "ٹھیک ہے، آرڈر نہیں کیا۔ آپ کا کارٹ محفوظ ہے؛ جب تیار ہوں یا کچھ بدلنا ہو تو بتائیں۔"},
    "login": {"latin": "Please log in to place your order. Your cart is saved.",
              "roman_urdu": "Order place karne ke liye login karein. Aap ka cart saved hai.",
              "urdu_script": "آرڈر کرنے کے لیے لاگ ان کریں۔ آپ کا کارٹ محفوظ ہے۔"},
}


_SET = {
    "payment": {"latin": "Done: you'll pay by {name}. Estimated total: {total}{tax}.",
                "roman_urdu": "Ho gaya: aap {name} se pay karenge. Andaazan total: {total}{tax}.",
                "urdu_script": "ہو گیا: آپ {name} سے ادائیگی کریں گے۔ اندازاً کل: {total}{tax}۔"},
    "payment_short": {"latin": "Done: you'll pay by {name}.", "roman_urdu": "Ho gaya: aap {name} se pay karenge.",
                      "urdu_script": "ہو گیا: آپ {name} سے ادائیگی کریں گے۔"},
    "choose": {"latin": "How would you like to pay?", "roman_urdu": "Aap kaise pay karna chahenge?",
               "urdu_script": "آپ کیسے ادائیگی کرنا چاہیں گے؟"},
    "online_note": {"latin": "Online methods are paid in the app's secure checkout.",
                    "roman_urdu": "Online methods ki payment app ke secure checkout mein hoti hai.",
                    "urdu_script": "آن لائن طریقوں کی ادائیگی ایپ کے محفوظ چیک آؤٹ میں ہوتی ہے۔"},
    "reply_pick": {"latin": "Reply with the number or the name.", "roman_urdu": "Number ya naam likh dein.",
                   "urdu_script": "نمبر یا نام لکھ دیں۔"},
    "next": {"latin": "Say \"place my order\" when you're ready.",
             "roman_urdu": "Tayyar hon to \"order place karo\" likhein.",
             "urdu_script": "تیار ہوں تو \"آرڈر کر دو\" لکھیں۔"},
    "wrong_type": {"latin": "{requested} isn't available for {kind}. You can pay with:",
                   "roman_urdu": "{kind} ke liye {requested} available nahi. Aap in se pay kar sakte hain:",
                   "urdu_script": "{kind} کے لیے {requested} دستیاب نہیں۔ آپ ان سے ادائیگی کر سکتے ہیں:"},
    "pick_one": {"latin": "Reply with the one you want.", "roman_urdu": "Jo chahiye uska naam likhein.",
                 "urdu_script": "جو چاہیے اس کا نام لکھیں۔"},
    "switched": {"latin": "Done, your order is now for {kind}.", "roman_urdu": "Ho gaya, ab aap ka order {kind} hai.",
                 "urdu_script": "ہو گیا، اب آپ کا آرڈر {kind} ہے۔"},
    "already": {"latin": "Your order is already set for {kind}.", "roman_urdu": "Aap ka order pehle se {kind} hai.",
                "urdu_script": "آپ کا آرڈر پہلے سے {kind} ہے۔"},
    "location": {"latin": "Sure, switching to delivery. Please allow your location so I can find the branch that "
                          "delivers to you.",
                 "roman_urdu": "Zaroor, delivery kar raha hoon. Apni location allow karein taake main woh branch "
                               "dhoondh sakoon jo aap tak deliver karti hai.",
                 "urdu_script": "ضرور، ڈیلیوری کر رہا ہوں۔ اپنی لوکیشن کی اجازت دیں تاکہ میں وہ برانچ ڈھونڈ سکوں جو آپ "
                                "تک ڈیلیور کرتی ہے۔"},
    "pickup_branch": {"latin": "Sure. This branch doesn't offer pickup, so please pick a branch for pickup from the "
                               "list.",
                      "roman_urdu": "Zaroor. Is branch par pickup nahi hai, list se pickup ke liye branch chunein.",
                      "urdu_script": "ضرور۔ اس برانچ پر پک اپ نہیں ہے، فہرست سے پک اپ کے لیے برانچ چنیں۔"},
    "cleared": {"latin": "{name} doesn't work for {kind}, so please tell me how you'd like to pay.",
                "roman_urdu": "{name} {kind} ke liye nahi hai, is liye batayein aap kaise pay karenge.",
                "urdu_script": "{name} {kind} کے لیے نہیں ہے، اس لیے بتائیں آپ کیسے ادائیگی کریں گے۔"},
}
KIND_WORDS = {"DELIVERY": {"latin": "delivery", "roman_urdu": "delivery", "urdu_script": "ڈیلیوری"},
              "PICKUP": {"latin": "pickup", "roman_urdu": "pickup", "urdu_script": "پک اپ"}}


def _s(key: str, lang: str) -> str:
    return _SET[key].get(lang) or _SET[key]["latin"]


def _kind(order_type: Optional[str], lang: str) -> str:
    return KIND_WORDS.get(order_type or "", {}).get(lang, (order_type or "").lower())


def options_text(methods: list, lang: str, intro_key: str) -> str:
    """Numbered payment choices with each estimated total, as code writes them (no invented taxes)."""
    rows = []
    for n, m in enumerate(methods, 1):
        price = f" – {rs(m['total'])}" if m.get("total") is not None else ""
        tax = _t("tax", lang).format(rate=m["tax_rate"]) if m.get("tax_rate") is not None and price else ""
        rows.append(f"{n}. {m['name']}{price}{tax}")
    parts = [_s(intro_key, lang), "\n".join(rows)]
    if any(m.get("pay_now") for m in methods):
        parts.append(_s("online_note", lang))
    return "\n".join(parts) + "\n\n" + _s("reply_pick", lang)


def not_ready_text(review: dict, lang: str) -> str:
    """Why the order can't be placed yet (code's words: the model invented missing branches and empty carts),
    with the payment choices when a payment method is what's missing."""
    problems = review["problems"]
    options = review.get("payment_options")
    rest = [p for p in problems if not (options and p["code"] in ("no_payment_method",))]
    text = problems_text(rest, lang) if rest else ""
    if options:
        text = (text + "\n\n" if text else "") + options_text(options, lang, "choose")
    return text


# orders.status -> what it means, in (English, Roman Urdu, Urdu script). Unknown codes fall back to the model.
STATUS = {
    "SUBMITTED": ("has reached our ordering system and is waiting for the branch to accept it",
                  "hamare ordering system tak pohnch gaya hai aur branch ke accept karne ka intezar hai",
                  "ہمارے آرڈرنگ سسٹم تک پہنچ گیا ہے اور برانچ کی منظوری کا انتظار ہے"),
    "PLACED": ("has been received by the branch", "branch ko mil gaya hai", "برانچ کو مل گیا ہے"),
    "PREPARING": ("is being prepared", "tayyar ho raha hai", "تیار ہو رہا ہے"),
    "READYATKITCHEN": ("is ready and waiting for the rider", "tayyar hai aur rider ka intezar hai",
                       "تیار ہے اور رائیڈر کا انتظار ہے"),
    "READYFORPICKUP": ("is ready for pickup", "pickup ke liye tayyar hai", "پک اپ کے لیے تیار ہے"),
    "WAITINGRIDER": ("is ready; a rider is being assigned", "tayyar hai; rider assign ho raha hai",
                     "تیار ہے؛ رائیڈر مقرر ہو رہا ہے"),
    "ASSIGNED": ("has a rider assigned", "ke liye rider assign ho gaya hai", "کے لیے رائیڈر مقرر ہو گیا ہے"),
    "PICKEDUP": ("is on the way to you", "aap ki taraf raste mein hai", "آپ کی طرف راستے میں ہے"),
    "DELIVERED": ("has been delivered", "deliver ho gaya hai", "پہنچا دیا گیا ہے"),
    "COMPLETED": ("is completed", "mukammal ho gaya hai", "مکمل ہو گیا ہے"),
    "CANCELED": ("was cancelled. For help, contact the Cheezious helpline",
                 "cancel ho gaya. Madad ke liye Cheezious helpline se rabta karein",
                 "منسوخ ہو گیا۔ مدد کے لیے Cheezious ہیلپ لائن سے رابطہ کریں"),
    "PAYMENTPENDING": ("is waiting for payment: please complete it in the app",
                       "payment ka intezar hai: app mein payment mukammal karein",
                       "ادائیگی کا انتظار ہے: ایپ میں ادائیگی مکمل کریں"),
    "HOLD": ("is on hold for now", "abhi hold par hai", "ابھی روکا گیا ہے"),
}
STATUS["REJECTED"] = STATUS["FAILED"] = STATUS["CANCELED"]
STATUS["PAYMENTFAILED"] = STATUS["PAYMENTPENDING"]
STATUS["FREEZE"] = STATUS["HOLD"]
_ORDER_HEAD = ("Your {kind}order from {branch}", "{branch} se aap ka {kind}order", "{branch} سے آپ کا {kind}آرڈر")
_NO_ORDER = ("I couldn't find an order on your account.", "Aap ke account par koi order nahi mila.",
             "آپ کے اکاؤنٹ پر کوئی آرڈر نہیں ملا۔")
_LOGIN = ("Please log in to see your orders.", "Apne orders dekhne ke liye login karein.",
          "اپنے آرڈر دیکھنے کے لیے لاگ ان کریں۔")


def status_text(result: dict, lang: str) -> Optional[str]:
    """One order's status in plain words (code's words: the model invented delivery times). No order ids."""
    i = {"latin": 0, "roman_urdu": 1, "urdu_script": 2}.get(lang, 0)
    if result.get("error") == "not_logged_in":
        return _LOGIN[i]
    if result.get("error") == "not_found":
        return _NO_ORDER[i]
    o = result.get("order") or {}
    phrase = STATUS.get((o.get("status") or "").upper())
    if not phrase:
        return None
    kind = KIND_WORDS.get(o.get("order_type") or "", {}).get(lang, "")
    head = _ORDER_HEAD[i].format(kind=f"{kind} " if kind else "", branch=o.get("branch") or "Cheezious")
    return f"{head} {phrase[i]}."


def action_text(result: dict, lang: str) -> Optional[str]:
    """Fixed reply for a payment choice or a delivery / pickup switch (the model invented totals and branches
    here). None: not one of these, or an outcome the model should word (ambiguous method, unknown method)."""
    if result.get("payment_method_selected"):
        review = result.get("review") or {}
        if review.get("ready"):  # the cart is ready: straight to the summary, no "now say place my order"
            return _s("payment_short", lang).format(name=result["name"]) + "\n\n" + summary_text(review, lang)
        tax = _t("tax", lang).format(rate=result["tax_rate"]) if result.get("tax_rate") is not None else ""
        total = result["total"] if result.get("total") is not None else result.get("cart_subtotal") or 0
        text = _s("payment", lang).format(name=result["name"], total=rs(total), tax=tax)
        if result.get("pay_now"):
            text += " " + _t("pay_now", lang)
        if review.get("problems"):  # something else is missing: say what, instead of "place my order" failing later
            return text + "\n\n" + problems_text(review["problems"], lang)
        return text + " " + _s("next", lang)
    if "methods" in result and "cart_subtotal" in result:  # get_payment_methods
        if not result["methods"]:
            return None  # none offered: the model explains
        return options_text(result["methods"], lang, "choose")
    if result.get("error") == "not_for_this_order_type":
        names = "\n".join(f"{n}. {o['name']}" for n, o in enumerate(result.get("available") or [], 1))
        return (_s("wrong_type", lang).format(requested=result["requested"], kind=_kind(result["order_type"], lang))
                + "\n" + names + "\n\n" + _s("pick_one", lang))
    if "order" in result and ("placed_in_this_chat" in result or "status" in (result.get("order") or {})) \
            or result.get("error") == "not_logged_in":
        return status_text(result, lang)
    if result.get("order_type_selected"):
        kind = _kind(result["order_type_selected"], lang)
        if result.get("unchanged"):
            return _s("already", lang).format(kind=kind)
        if result.get("needs"):
            text = _s(result["needs"], lang)
        else:
            text = _s("switched", lang).format(kind=kind)
        if result.get("payment_cleared"):
            text += " " + _s("cleared", lang).format(name=result["payment_cleared"], kind=kind)
        return text
    return None


PROBLEMS = {  # (English, Roman Urdu, Urdu script)
    "empty_cart": ("your cart is empty: add something first", "aap ka cart khali hai, pehle kuch add karein",
                   "آپ کا کارٹ خالی ہے، پہلے کچھ شامل کریں"),
    "unavailable_items": ("not available right now: {items} (remove them first)",
                          "abhi available nahi: {items} (pehle inhein hatayein)", "ابھی دستیاب نہیں: {items} (پہلے انہیں ہٹائیں)"),
    "branch_closed": ("the branch isn't taking orders right now", "branch abhi orders nahi le rahi",
                      "برانچ ابھی آرڈر نہیں لے رہی"),
    "below_minimum_order": ("the minimum order is {minimum}", "minimum order {minimum} hai", "کم از کم آرڈر {minimum} ہے"),
    "no_order_type": ("choose delivery or pickup", "delivery ya pickup chunein", "ڈیلیوری یا پک اپ چنیں"),
    "pickup_not_offered": ("this branch doesn't offer pickup", "is branch par pickup nahi hai", "اس برانچ پر پک اپ نہیں ہے"),
    "no_delivery_location": ("share your location for delivery", "delivery ke liye location share karein",
                             "ڈیلیوری کے لیے لوکیشن شیئر کریں"),
    "no_payment_method": ("choose how you'll pay", "payment ka tareeqa chunein", "ادائیگی کا طریقہ چنیں"),
    "payment_method_wrong_type": ("your payment method doesn't fit this order type, please choose again",
                                  "yeh payment is order type ke liye nahi, dobara chunein",
                                  "یہ ادائیگی اس آرڈر کے لیے نہیں، دوبارہ چنیں"),
    "payment_method_unavailable": ("your payment method isn't available any more, please choose again",
                                   "yeh payment ab available nahi, dobara chunein", "یہ ادائیگی اب دستیاب نہیں، دوبارہ چنیں"),
    "login_required": ("please log in to place it", "order ke liye login karein", "آرڈر کے لیے لاگ ان کریں"),
}
_HEAD = ("Almost there! Before I can place your order:", "Bas thora sa reh gaya! Order se pehle:",
         "بس تھوڑا سا رہ گیا! آرڈر سے پہلے:")


def problems_text(problems: list, lang: str) -> str:
    """What's missing before the order can be placed, as a short list."""
    i = {"latin": 0, "roman_urdu": 1, "urdu_script": 2}.get(lang, 0)
    rows = [PROBLEMS.get(p["code"], ("something needs fixing",) * 3)[i].format(
        items=", ".join(p.get("items") or []), minimum=rs(p.get("minimum") or 0)) for p in problems]
    return _HEAD[i] + "\n" + "\n".join(f"- {r}" for r in rows)


def outcome_text(key: str, lang: str, *, order_type: Optional[str] = None, pay_now: bool = False,
                 message: Optional[str] = None) -> str:
    """What the bot says after the customer's yes (or no). Never contains the order id."""
    text = _DONE[key].get(lang) or _DONE[key]["latin"]
    if key == "placed":
        how = _DONE[order_type].get(lang, "") if order_type in payments.ORDER_TYPES else ""
        if pay_now:
            how = f"{how} {_DONE['pay_now'].get(lang) or _DONE['pay_now']['latin']}".strip()
        return text.format(how=how).replace("  ", " ")
    if key == "rejected":
        return text.format(why=f" ({message.strip()[:160]})" if message and message.strip() else "")
    return text


# ---------- cart outcomes (the model stated wrong totals: "1 Crown Crust, Rs 2050" for a cart of 2, Rs 4,100) ----------

_CART = {
    "added": ("Added {what} to your cart.", "{what} cart mein daal diya.", "{what} کارٹ میں شامل کر دیا۔"),
    "removed": ("Removed {what} from your cart.", "{what} cart se hata diya.", "{what} کارٹ سے ہٹا دیا۔"),
    "updated": ("Done: {what} now.", "Ho gaya: ab {what}.", "ہو گیا: اب {what}۔"),
    "cleared": ("Your cart is now empty.", "Aap ka cart ab khali hai.", "آپ کا کارٹ اب خالی ہے۔"),
    "empty": ("Your cart is empty. What would you like to order?", "Aap ka cart khali hai. Kya order karna chahenge?",
              "آپ کا کارٹ خالی ہے۔ کیا آرڈر کرنا چاہیں گے؟"),
    "head": ("Your cart:", "Aap ka cart:", "آپ کا کارٹ:"),
    "subtotal": ("Subtotal: {total} (tax and delivery are added at checkout).",
                 "Subtotal: {total} (tax aur delivery checkout par lagte hain).",
                 "کل: {total} (ٹیکس اور ڈیلیوری چیک آؤٹ پر شامل ہوں گے)۔"),
    "next": ("Anything else, or shall I place the order?", "Kuch aur chahiye, ya order place kar doon?",
             "کچھ اور چاہیے، یا آرڈر کر دوں؟"),
    "not_in_cart": ("That isn't in your cart.", "Yeh aap ke cart mein nahi hai.", "یہ آپ کے کارٹ میں نہیں ہے۔"),
    "na": ("not available now", "abhi available nahi", "ابھی دستیاب نہیں"),
}


PLACE_QUESTIONS = _CART["next"]  # the cart reply's "...or shall I place the order?": a "yes" to it orders


def _i(lang: str) -> int:
    return {"latin": 0, "roman_urdu": 1, "urdu_script": 2}.get(lang, 0)


def _line(l: dict) -> str:
    return f"{l['qty']} × {l['name']}" + (f" ({l['size']})" if l.get("size") else "")


def cart_summary(cart: dict, lang: str) -> str:
    """The cart exactly as priced (app/cart.py price_cart), as a Markdown list with the subtotal."""
    i = _i(lang)
    if not cart or not cart.get("lines"):
        return _CART["empty"][i]
    rows = [f"- {_line(l)} – " + (rs(l["line_total"]) if l.get("line_total") is not None else _CART["na"][i])
            for l in cart["lines"]]
    return "\n\n".join([_CART["head"][i], "\n".join(rows), _CART["subtotal"][i].format(total=rs(cart["subtotal"]))])


def cart_text(result: dict, lang: str, question: Optional[str] = None) -> Optional[str]:
    """Code's reply for a cart change or a look at the cart; question = what the cart still needs to ask (a size,
    which one). None: not a cart result this handles (the model words it)."""
    i = _i(lang)
    cart = result.get("cart")
    if cart is None:
        return None
    if result.get("error") == "not_in_cart":
        return _CART["not_in_cart"][i] + "\n\n" + cart_summary(cart, lang)
    if "added" in result:
        if not result["added"]:
            return question  # nothing added: only the question (or None: the model explains)
        done = _CART["added"][i].format(what=", ".join(_line(a) for a in result["added"]))
    elif "removed" in result:
        done = _CART["removed"][i].format(what=_line(result["removed"]))
    elif "updated" in result:
        line = next((l for l in cart["lines"] if l["line_no"] == result["updated"]["line_no"]), None)
        done = _CART["updated"][i].format(what=_line(line)) if line else ""
    elif result.get("cleared"):
        return _CART["cleared"][i]
    elif set(result) <= {"cart", "as_of"}:  # view_cart
        summary = cart_summary(cart, lang)
        return summary + ("\n\n" + _CART["next"][i] if cart.get("lines") else "")
    else:
        return None
    parts = [done, cart_summary(cart, lang)]
    parts.append(question if question else _CART["next"][i])
    return "\n\n".join(p for p in parts if p)
