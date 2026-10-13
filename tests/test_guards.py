from app.agent.guards import amounts_in, check_answer, detect_language, pre_check, redact


RESULTS = [{"items": [{"name": "Chicken Tikka", "sizes": [{"size": "Small", "price": 690}, {"size": "Large", "price": 1850}]}]}]


def test_amount_formats():
    assert amounts_in("Rs 1 350, Rs. 690/-, 1,850 PKR, ₨ 3000, 750 روپے") == [1350, 690, 1850, 3000, 750]


def test_grounded_price_passes():
    assert check_answer("Large is Rs 1850, small Rs 690.", RESULTS, ["search_menu"], "price?") == []


def test_invented_price_fails():
    assert check_answer("Large is Rs 1900.", RESULTS, ["search_menu"], "price?")


def test_price_without_tool_fails():
    assert check_answer("It costs Rs 690.", [], [], "price?")


def test_budget_from_user_allowed():
    assert check_answer("Nothing under Rs 800, sorry.", RESULTS, ["search_menu"], "spicy under 800") == []


def test_order_status_without_tool_fails():
    assert check_answer("Your order has been delivered.", [], [], "where is my order")


def test_redaction():
    assert redact("call 0300-1234567 or a@b.com") == "call <phone> or <email>"


def test_pre_check():
    assert pre_check("x" * 1001) is None
    assert pre_check("hi\x00\x07 there") == "hi there"


def test_language_detection():
    from app.agent.guards import LATIN, MIXED, ROMAN_URDU_LANG, URDU_SCRIPT
    assert detect_language("is the branch open?") == LATIN  # never forced to English; model mirrors
    assert detect_language("aur large wala kitne ka hai?") == ROMAN_URDU_LANG
    assert detect_language("sab sa zayada selling item kon si ha") == ROMAN_URDU_LANG
    assert detect_language("bhai koi deal hai for 4 people?") == ROMAN_URDU_LANG
    assert detect_language("Kya you have deals?") == MIXED
    assert detect_language("Do you have pizza or burger?") == LATIN
    assert detect_language("میرا آرڈر کہاں ہے؟") == URDU_SCRIPT
