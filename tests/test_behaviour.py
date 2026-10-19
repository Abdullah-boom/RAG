"""Behaviour layer: greetings, thanks, insults, identity and off-topic chat never reach the tools."""
import pytest

from app.agent.behaviour import ABUSE, BYE, GREETING, IDENTITY, OFF_TOPIC, THANKS, kind, reply


def no_router(prompt):
    raise AssertionError("the model router should not be needed for this message")


@pytest.mark.parametrize("message,expected", [
    ("tum 1 kuttay ho", ABUSE), ("mujhe btao kay tum kutay hu", ABUSE), ("lun py chroooo", ABUSE),
    ("tu kutte ka bacha hai", ABUSE), ("کتے", ABUSE), ("shut up", ABUSE),
    ("hi", GREETING), ("Hello!", GREETING), ("salam", GREETING), ("aoa bhai", GREETING), ("السلام علیکم", GREETING),
    ("shukriya", THANKS), ("thanks a lot", THANKS), ("ok bye", BYE), ("Allah hafiz", BYE),
    ("who are you?", IDENTITY), ("tum kaun ho", IDENTITY), ("آپ کون ہیں", IDENTITY),
])
def test_clear_cases_need_no_model(message, expected):
    assert kind(message, in_conversation=False, ask_model=no_router) == expected


@pytest.mark.parametrize("message", [
    "kuch khatta ha?", "kuch cheesy ha", "hi, pizza kitne ki hai?", "bc pizza dikhao jaldi", "pizza ghatiya tha",
    "mera order kahan hai", "cart dikhao", "deals", "bhook lagi hai", "zinger hai?", "thanks, add the large one",
])
def test_business_messages_go_to_the_agent(message):
    assert kind(message, in_conversation=False, ask_model=no_router) is None


@pytest.mark.parametrize("message", ["2", "haan", "pehli wali", "nahi rehne do"])
def test_short_replies_mid_conversation_go_to_the_agent(message):
    assert kind(message, in_conversation=True, ask_model=no_router) is None


def test_router_decides_unclear_messages_and_fails_open():
    assert kind("aaj mausam kaisa hai", False, ask_model=lambda p: "OTHER") == OFF_TOPIC
    assert kind("aaj mausam kaisa hai", False, ask_model=lambda p: "FOOD") is None
    def down(prompt):
        raise TimeoutError
    assert kind("aaj mausam kaisa hai", False, ask_model=down) is None


def test_replies_follow_the_customer_language():
    assert reply(GREETING, "latin", "hi").startswith("Hi!")
    assert reply(GREETING, "latin", "salam").startswith("Walaikum Assalam!")
    assert reply(ABUSE, "roman_urdu", "tum 1 kuttay ho").startswith("Main aap ki madad")
    assert reply(IDENTITY, "urdu_script", "آپ کون ہیں").startswith("میں")


def test_price_words_alone_are_not_food():
    asked = []
    assert kind("2+2 kitne hote hain", False, ask_model=lambda p: asked.append(p) or "OTHER") == OFF_TOPIC and asked
    assert kind("kitne ki hai?", True, ask_model=no_router) is None  # after a list: a price question


@pytest.mark.parametrize("message", ["shukriya", "lun py chroooo", "Allah hafiz"])
def test_roman_urdu_words_get_roman_urdu_replies(message):
    k = kind(message, False, ask_model=no_router)
    assert not reply(k, "latin", message).startswith(("You're", "I'm", "Thanks"))
