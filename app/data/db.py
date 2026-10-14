"""Read-only MongoDB access (spec 7.2): secondary-preferred reads, short timeouts, small pool."""
import time
from functools import lru_cache

from pymongo import MongoClient, ReadPreference
from pymongo.errors import PyMongoError

from app import config

QUERY_TIMEOUT_MS = 3000
RETRY_WAIT_S = 0.5


@lru_cache(maxsize=1)
def client() -> MongoClient:
    return MongoClient(
        config.MONGO_URI,
        read_preference=ReadPreference.SECONDARY_PREFERRED,
        maxPoolSize=5,
        serverSelectionTimeoutMS=10000,
        socketTimeoutMS=10000,
    )


def product_db():
    return client()["product"]


def order_db():
    return client()["order"]


def faq_collection():
    return client()["aggregator"]["faqs"]


def with_retry(fn, *args, **kwargs):
    """Run a call that reads Mongo, with one retry for a dropped Atlas connection.
    A second failure raises PyMongoError: callers turn it into a clear error, never a partial answer."""
    try:
        return fn(*args, **kwargs)
    except PyMongoError:
        time.sleep(RETRY_WAIT_S)
        return fn(*args, **kwargs)
