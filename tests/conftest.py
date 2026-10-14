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
