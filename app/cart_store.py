"""One cart per (owner, branch), kept apart from chat conversations.

owner is "user:<verified id>" for a logged-in customer, else "guest:<server-minted id>". The key holds the
branch, so an operation for one branch can never read or change another branch's cart, and switching
branch, starting a new chat or reopening the app keeps every cart as it was.

Lines are {item_id, name, size, qty}; prices are never stored (app/cart.py re-prices on every read).
Next to each cart: the payment method the customer picked for it (a code like "COD"; see app/payments.py),
the order type (DELIVERY / PICKUP, with the delivery spot for a delivery) and, between "here's your order,
confirm?" and the customer's yes, the pending confirm token. Per owner (all branches): the orders the bot placed
({order_id, order_no, branch_id, placed_at}), kept server side only so the bot can track them; never sent to the
app.
Redis when REDIS_URL is set (survives restarts, shared by workers, expires after CART_TTL_DAYS idle);
otherwise process memory (dev only). Every change runs under a per-cart lock, so a chat turn and a cart
button pressed at the same time can't overwrite each other's change.
"""
import json
import threading
from contextlib import contextmanager
from functools import lru_cache
from typing import Dict, Iterator, List, Optional

from app import config

LOCK_TTL_S = 120  # a crashed worker's lock frees itself after this
LOCK_WAIT_S = 45  # longer than one chat turn, so a cart button waits for the turn instead of failing


class CartBusy(Exception):
    """Another request kept this cart locked for longer than LOCK_WAIT_S."""


class CartUnavailable(Exception):
    """The cart store (Redis) could not be reached."""


def owner_key(user_id: Optional[str], guest_id: Optional[str]) -> str:
    if user_id:
        return f"user:{user_id}"
    if not guest_id:
        raise ValueError("a guest cart needs a guest id")
    return f"guest:{guest_id}"


def _key(owner: str, branch_id: str) -> str:
    return f"cart:{branch_id}:{owner}"


def _pay_key(owner: str, branch_id: str) -> str:
    return f"pay:{branch_id}:{owner}"


def _meta_key(kind: str, owner: str, branch_id: Optional[str] = None) -> str:
    return f"{kind}:{branch_id}:{owner}" if branch_id else f"{kind}:{owner}"


MAX_PLACED = 10  # orders remembered per owner for tracking, newest first


class _Meta:
    """Small JSON values next to the carts, on top of a store's _get_json / _set_json."""

    def get_order(self, owner: str, branch_id: str) -> Optional[dict]:
        """{"type": "DELIVERY" | "PICKUP", "lat"?, "lng"?} or None (not chosen yet)."""
        return self._get_json(_meta_key("order", owner, branch_id))

    def set_order(self, owner: str, branch_id: str, order: Optional[dict]) -> None:
        self._set_json(_meta_key("order", owner, branch_id), order)

    def get_confirm(self, owner: str, branch_id: str) -> Optional[str]:
        return self._get_json(_meta_key("confirm", owner, branch_id))

    def set_confirm(self, owner: str, branch_id: str, token: Optional[str]) -> None:
        self._set_json(_meta_key("confirm", owner, branch_id), token)

    def placed_orders(self, owner: str) -> List[dict]:
        return self._get_json(_meta_key("placed", owner)) or []

    def add_placed(self, owner: str, order: dict) -> None:
        self._set_json(_meta_key("placed", owner), ([order] + self.placed_orders(owner))[:MAX_PLACED])


class MemoryCartStore(_Meta):
    def __init__(self) -> None:
        self._carts: Dict[str, List[dict]] = {}
        self._payments: Dict[str, str] = {}
        self._meta: Dict[str, str] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get(self, owner: str, branch_id: str) -> List[dict]:
        return [dict(line) for line in self._carts.get(_key(owner, branch_id), [])]

    def set(self, owner: str, branch_id: str, lines: List[dict]) -> None:
        if lines:
            self._carts[_key(owner, branch_id)] = [dict(line) for line in lines]
        else:
            self._carts.pop(_key(owner, branch_id), None)

    def get_payment(self, owner: str, branch_id: str) -> Optional[str]:
        return self._payments.get(_pay_key(owner, branch_id))

    def set_payment(self, owner: str, branch_id: str, code: Optional[str]) -> None:
        if code:
            self._payments[_pay_key(owner, branch_id)] = code
        else:
            self._payments.pop(_pay_key(owner, branch_id), None)

    def _get_json(self, key: str):
        raw = self._meta.get(key)
        return json.loads(raw) if raw else None

    def _set_json(self, key: str, value) -> None:
        if value:
            self._meta[key] = json.dumps(value)
        else:
            self._meta.pop(key, None)

    @contextmanager
    def lock(self, owner: str, branch_id: str) -> Iterator[None]:
        with self._guard:
            lock = self._locks.setdefault(_key(owner, branch_id), threading.Lock())
        if not lock.acquire(timeout=LOCK_WAIT_S):
            raise CartBusy()
        try:
            yield
        finally:
            lock.release()


@contextmanager
def _redis_errors() -> Iterator[None]:
    import redis
    try:
        yield
    except redis.exceptions.LockError:
        raise
    except redis.exceptions.RedisError as e:
        raise CartUnavailable() from e


class RedisCartStore(_Meta):
    def __init__(self, url: str, ttl_days: int) -> None:
        import redis
        self._r = redis.Redis.from_url(url, socket_timeout=3, socket_connect_timeout=3)
        self._ttl = ttl_days * 86400

    def get(self, owner: str, branch_id: str) -> List[dict]:
        with _redis_errors():
            raw = self._r.get(_key(owner, branch_id))
        return json.loads(raw) if raw else []

    def set(self, owner: str, branch_id: str, lines: List[dict]) -> None:
        with _redis_errors():
            if lines:
                self._r.set(_key(owner, branch_id), json.dumps(lines), ex=self._ttl)
            else:
                self._r.delete(_key(owner, branch_id))

    def get_payment(self, owner: str, branch_id: str) -> Optional[str]:
        with _redis_errors():
            raw = self._r.get(_pay_key(owner, branch_id))
        return raw.decode() if raw else None

    def set_payment(self, owner: str, branch_id: str, code: Optional[str]) -> None:
        with _redis_errors():
            if code:
                self._r.set(_pay_key(owner, branch_id), code, ex=self._ttl)
            else:
                self._r.delete(_pay_key(owner, branch_id))

    def _get_json(self, key: str):
        with _redis_errors():
            raw = self._r.get(key)
        return json.loads(raw) if raw else None

    def _set_json(self, key: str, value) -> None:
        with _redis_errors():
            if value:
                self._r.set(key, json.dumps(value), ex=self._ttl)
            else:
                self._r.delete(key)

    @contextmanager
    def lock(self, owner: str, branch_id: str) -> Iterator[None]:
        import redis
        # thread_local=False: a streamed chat turn may release the lock from another worker thread.
        lock = self._r.lock(f"lock:{_key(owner, branch_id)}", timeout=LOCK_TTL_S, blocking_timeout=LOCK_WAIT_S,
                            thread_local=False)
        with _redis_errors():
            acquired = lock.acquire()
        if not acquired:
            raise CartBusy()
        try:
            yield
        finally:
            try:
                lock.release()
            except redis.exceptions.RedisError:  # expired after LOCK_TTL_S, or Redis gone: it frees itself
                pass


@lru_cache(maxsize=1)
def store():
    if config.REDIS_URL:
        return RedisCartStore(config.REDIS_URL, config.CART_TTL_DAYS)
    return MemoryCartStore()
