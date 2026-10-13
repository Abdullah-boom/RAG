"""Read-only MongoDB access (spec 7.2): secondary-preferred reads, short timeouts, small pool."""
from functools import lru_cache

from pymongo import MongoClient, ReadPreference

from app import config

QUERY_TIMEOUT_MS = 3000


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
