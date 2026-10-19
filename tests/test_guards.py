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
    assert detect_language("ma apni girlfriend to kiya khilaon") == ROMAN_URDU_LANG
    assert detect_language("Kya you have deals?") == MIXED
    assert detect_language("Do you have pizza or burger?") == LATIN
    assert detect_language("میرا آرڈر کہاں ہے؟") == URDU_SCRIPT


def test_branch_status_needs_branch_tool():
    from app.agent.guards import check_answer
    claim = "The Gulberg branch is currently open, and pickup is available."
    assert check_answer(claim, [], [], "is my branch open?")
    assert not check_answer(claim, [{"open_now": True}], ["check_branch_status"], "is my branch open?")
    # cart replies mention delivery fees: not a branch claim
    assert not check_answer("Subtotal Rs 690. Tax and delivery are added at checkout.", [{"subtotal": 690}],
                            ["add_to_cart"], "add it")


def test_describing_items_needs_a_lookup():
    from app.agent.guards import check_answer
    names = ["beef pepperoni thin crust", "chicken tikka"]
    text = "The Beef Pepperoni Thin Crust has mozzarella and tomato sauce."
    assert check_answer(text, [], [], "what's in it?", names)
    assert not check_answer(text, [{"item": {}}], ["get_item_details"], "what's in it?", names)
    assert not check_answer("I can help with our menu, deals and orders.", [], [], "tell me a joke", names)


def test_urdu_script_reply_to_english_is_rejected():
    from app.agent.guards import check_answer
    urdu = "معاف کیجئے، ہمارے مینو میں سوشی دستیاب نہیں ہے۔"
    assert check_answer(urdu, [{"items": []}], ["search_menu"], "do you have sushi?")
    assert not check_answer(urdu, [{"items": []}], ["search_menu"], "کیا سوشی ہے؟")
    assert not check_answer("Sorry, sushi isn't on our menu.", [{"items": []}], ["search_menu"], "do you have sushi?")


def test_inexact_match_must_be_answered_with_uncertainty():
    from app.agent.guards import check_answer
    results = [{"match": "several", "exact": False, "items": [{"name": "Chicken Tikka", "price_from": 660}],
                "spelling_assumed": {"tiqqa": "tikka"}}]
    sure = "Chicken Tikka pizza is Rs 660."
    unsure = "I couldn't find 'chicken tiqqa' exactly. Did you mean the Chicken Tikka pizza (from Rs 660)?"
    assert check_answer(sure, results, ["search_menu"], "how much is chicken tiqqa")
    assert not check_answer(unsure, results, ["search_menu"], "how much is chicken tiqqa")
    assert not check_answer("Kya aapka matlab Chicken Tikka pizza hai? Rs 660 se shuru.", results, ["search_menu"], "chicken tiqqa")


def test_several_matches_must_be_listed_not_picked():
    from app.agent.guards import check_answer
    results = [{"match": "several", "items": [{"name": "Chicken Tikka", "price_from": 690},
                                              {"name": "Malai Tikka", "price_from": 1530}]}]
    picked = "Did you mean our Chicken Tikka pizza? It starts at Rs 690."
    listed = "Did you mean the Chicken Tikka pizza (from Rs 690) or the Malai Tikka pizza (from Rs 1530)?"
    assert check_answer(picked, results, ["search_menu"], "chicken tikka")
    assert not check_answer(listed, results, ["search_menu"], "chicken tikka")


def test_roman_urdu_question_needs_roman_urdu_reply():
    from app.agent.guards import check_answer
    res = [{"deals": [{"name": "Small Pizza Deal", "price": 750}]}]
    english = "Here are deals under Rs 1500: Small Pizza Deal for Rs 750. Let me know which one you want."
    roman = "Ji haan, 1500 se kam ye deal hai: Small Pizza Deal Rs 750 ki hai. Aap kaunsi lena chahenge?"
    assert check_answer(english, res, ["get_deals"], "koi deal hai 1500 se kam?")
    assert not check_answer(roman, res, ["get_deals"], "koi deal hai 1500 se kam?")
    assert not check_answer(english, res, ["get_deals"], "any deals under 1500?")


def test_cards_match_names_despite_odd_spaces():
    import app.agent.graph as g
    res = [{"items": [{"name": "Somewhat Amazing 1", "price": 1250}, {"name": "Euro", "price": 500}]}]
    cards = g._cards("1. **Somewhat Amazing 1** – Rs 1250", res)
    assert [c["name"] for c in cards] == ["Somewhat Amazing 1"]  # and "Euro" is not matched inside other words


def test_made_up_items_are_caught_real_ones_pass():
    from app.agent.guards import check_answer
    menu = ["bazinga burger", "reggy burger", "fettuccine alfredo pasta", "crunchy chicken pasta"]
    made_up = "We have two pastas:\n1. **Spaghetti Regular** – ask for price\n2. **Fettuccine Alfredo Pasta** – Rs 1050"
    problems = check_answer(made_up, [{"items": [{"name": "Fettuccine Alfredo Pasta", "price": 1050}]}],
                            ["search_menu"], "pasta kon si hai", menu)
    assert any("Spaghetti Regular" in p for p in problems)
    real = "- **Bazinga Burger** – Rs 560\n- **Reggy Burger** – Rs 390\nPay by **Cash on Delivery**. Status: READYATKITCHEN"
    results = [{"items": [{"name": "Bazinga Burger", "price": 560}, {"name": "Reggy Burger", "price": 390}]}]
    assert check_answer(real, results, ["search_menu"], "burgers?", menu) == []
    assert check_answer("Our **I-8 Islamabad** branch is open.", [{"open_now": True}], ["check_branch_status"],
                        "open?", menu, ["I-8 Islamabad"]) == []


def test_text_tool_call_is_read_as_a_call():
    from app.agent.graph import _text_tool_call
    names = {"search_menu"}
    text = '{"name": "search_menu", "parameters": {"query": "sweet", "deals_only": False, "max_price": null}}'
    assert _text_tool_call(text, names) == {"name": "search_menu",
                                            "args": {"query": "sweet", "deals_only": False, "max_price": None}}
    assert _text_tool_call('```json\n{"name": "search_menu", "arguments": {"query": "x"}}\n```', names)["args"] == {"query": "x"}
    assert _text_tool_call('{"name": "delete_everything", "parameters": {}}', names) is None
    assert _text_tool_call("Aap ke liye yeh options hain", names) is None
    assert _text_tool_call('[1, 2]', names) is None


def test_nothing_on_menu_only_for_plain_searches():
    from app.agent.graph import _nothing_on_menu
    empty = {"items": [], "note": "no_match"}
    plain = {"tool": "search_menu", "args": {"query": "sour", "max_price": "None", "deals_only": "False", "limit": "10"},
             "result": empty}
    assert _nothing_on_menu([plain, {"draft": "x", "guard_problems": ["y"]}])
    assert not _nothing_on_menu([{**plain, "args": {"query": "sour", "max_price": "800"}}])  # nothing under a budget
    assert not _nothing_on_menu([{**plain, "args": {"query": "sour", "deals_only": True}}])
    assert not _nothing_on_menu([plain, {"tool": "search_menu", "args": {"query": "lemon"}, "result": {"items": [{}]}}])
    assert not _nothing_on_menu([{"tool": "get_deals", "args": {}, "result": empty}])
    assert not _nothing_on_menu([])


def test_placeholder_instead_of_a_value_fails():
    from app.agent.guards import check_answer
    cart = [{"cart": {"subtotal": 2610}}]
    problems = check_answer("Total: Rs 2610. With tax it comes to Rs [calculate_total].", cart, ["view_cart"], "cart")
    assert any("placeholder" in p for p in problems)
    assert not check_answer("Total: Rs 2610. Tax and delivery are added at checkout.", cart, ["view_cart"], "cart")
