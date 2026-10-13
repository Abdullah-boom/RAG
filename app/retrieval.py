"""Hybrid retrieval over the local index (spec 8): dense (multilingual) + BM25, fused by RRF.

Vector search only decides WHICH items are relevant. Facts are re-read from MongoDB by id.
"""
import json
import re
from functools import lru_cache
from typing import List, Optional

import numpy as np
import yaml
from rank_bm25 import BM25Okapi

from app import config

RRF_K = 60
MIN_COSINE = 0.40  # below this a dense-only hit counts as "nothing found"
TOKEN = re.compile(r"\w+", re.UNICODE)


@lru_cache(maxsize=1)
def _model():
    from fastembed import TextEmbedding
    return TextEmbedding(config.EMBED_MODEL)


def embed(texts: List[str]) -> np.ndarray:
    v = np.array(list(_model().embed(texts)), dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def _tokens(text: str) -> List[str]:
    return TOKEN.findall(text.lower())


@lru_cache(maxsize=1)
def _synonyms() -> dict:
    path = config.ROOT / "app" / "ingestion" / "synonyms.yaml"
    return yaml.safe_load(path.read_text()) or {}


def expand_query(query: str) -> str:
    q = query.lower()
    extra = [w for key, words in _synonyms().items() if re.search(rf"\b{re.escape(key)}\b", q) for w in words]
    return " ".join([query] + extra)


class Index:
    def __init__(self) -> None:
        data = json.loads((config.INDEX_DIR / "docs.json").read_text())
        if data["embed_model"] != config.EMBED_MODEL:
            raise RuntimeError("Index built with a different embedding model; run the sync again.")
        self.docs = data["docs"]
        self.vectors = np.load(config.INDEX_DIR / "vectors.npy")
        self.bm25 = BM25Okapi([_tokens(d["text"]) for d in self.docs])

    def search(self, query: str, kind: str, k: int = 8, branch_id: Optional[str] = None,
               max_price: Optional[float] = None, deals_only: bool = False) -> List[dict]:
        allowed = []
        for i, d in enumerate(self.docs):
            m = d["metadata"]
            if m["kind"] != kind:
                continue
            if branch_id and kind == "menu" and branch_id not in m["branch_ids"]:
                continue
            if max_price is not None and (m.get("price_min") is None or m["price_min"] > max_price):
                continue
            if deals_only and not m.get("is_deal"):
                continue
            allowed.append(i)
        if not allowed:
            return []
        expanded = expand_query(query)
        cos = self.vectors[allowed] @ embed([expanded])[0]
        bm = self.bm25.get_scores(_tokens(expanded))[allowed]
        dense_rank = {allowed[j]: r for r, j in enumerate(np.argsort(-cos))}
        sparse_rank = {allowed[j]: r for r, j in enumerate(np.argsort(-bm)) if bm[j] > 0}
        cos_of = dict(zip(allowed, cos))
        q_tokens = set(_tokens(query))
        name_hit = {i for i in allowed if kind == "menu"
                    and set(_tokens(self.docs[i]["metadata"]["name"])) <= q_tokens}  # exact item name in query
        filtered = max_price is not None or deals_only  # filters already define relevance
        scored = []
        for i in allowed:
            if not filtered and i not in sparse_rank and cos_of[i] < MIN_COSINE and i not in name_hit:
                continue  # no result is a valid result
            s = 1 / (RRF_K + dense_rank[i]) + (1 / (RRF_K + sparse_rank[i]) if i in sparse_rank else 0)
            s += 1.0 if i in name_hit else 0
            scored.append((s, i))
        scored.sort(reverse=True)
        return [dict(self.docs[i]["metadata"], score=round(s, 5)) for s, i in scored[:k]]


@lru_cache(maxsize=1)
def index() -> Index:
    return Index()
