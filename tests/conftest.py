import warnings

import pytest

warnings.filterwarnings("ignore")


@pytest.fixture(autouse=True)
def fresh_cart_store(monkeypatch):
    """Every test gets its own empty in-memory cart store (never the dev process's or Redis)."""
    from app import api, cart_store
    s = cart_store.MemoryCartStore()
    monkeypatch.setattr(api, "cart_store", lambda: s)
    return s


@pytest.fixture(autouse=True)
def no_payment_config(monkeypatch):
    """Cart responses add a payment estimate; tests that need real methods patch api.payment_config."""
    from app import api
    monkeypatch.setattr(api, "payment_config", lambda branch_id: None)


@pytest.fixture(autouse=True)
def router_says_food(monkeypatch):
    """Graph tests script the model's replies; the behaviour layer's FOOD / OTHER call must not use one up.
    tests/test_behaviour.py passes its own ask_model to test the router itself."""
    from app.agent import graph
    monkeypatch.setattr(graph, "_ask_router", lambda prompt: "FOOD")


@pytest.fixture(autouse=True)
def no_plan(monkeypatch):
    """The understanding call is off by default (the word-list path runs, as when the call fails); graph tests
    script _invoke_llm only. tests/test_understand.py patches _plan_message to test the plan routes."""
    from app.agent import graph
    monkeypatch.setattr(graph, "_plan_message", lambda prompt: None)
