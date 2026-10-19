"""Hybrid retrieval over the local index (spec 8): dense (multilingual) + BM25, fused by RRF.

Vector search only decides WHICH items are relevant. Facts are re-read from MongoDB by id.
"""
import difflib
import json
import re
from functools import lru_cache
from typing import List, Optional

import numpy as np
import yaml
from rank_bm25 import BM25Okapi

from app import config

# Words that say what kind of food it is, not which item: "chicken tikka" should find every tikka item,
# not every chicken item. Used only by keyword_matches.
GENERIC_WORDS = {
    "chicken", "beef", "pizza", "pizzas", "burger", "burgers", "deal", "deals", "meal", "meals", "combo",
    "spicy", "hot", "large", "small", "regular", "medium", "party", "price", "prices", "much", "how", "the",
    "and", "with", "for", "item", "items", "menu", "flavour", "flavor", "one", "some", "something", "any",
    "have", "you", "your", "what", "show", "options", "option", "want", "get", "please", "order",
    # Roman Urdu filler: "tikka wala kuch hai?" means "anything with tikka?"
    "wala", "wali", "wale", "kuch", "koi", "hai", "hain", "kya", "kitne", "kitna", "kitni", "mein", "main",
    "aur", "bhi", "chahiye", "dikhao", "batao", "dein", "do", "milta", "milti", "milte", "bhai", "yaar",
    "se", "kam", "zyada", "sasta", "under", "below", "less", "than", "cheap", "cheapest", "best", "good",
}
TYPO_CUTOFF = 0.8  # similarity needed to read an unknown word as a menu word
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


def _sound(word: str) -> str:
    """Roman Urdu / English spelling-insensitive form: tiqqa ~ tikka, bazooqa ~ bazooka, nugets ~ nuggets."""
    w = word.lower().replace("q", "k").replace("ee", "i").replace("oo", "u").replace("ph", "f").replace("v", "w")
    return re.sub(r"(.)\1+", r"\1", w)


def expand_query(query: str) -> str:
    q = query.lower()
    keys = [key for key in _synonyms() if re.search(rf"\b{re.escape(key)}\b", q)]
    # The longer key wins: "sweet corn" is corn, not "sweet" (dessert).
    keys = [k for k in keys if not any(k != o and re.search(rf"\b{re.escape(k)}\b", o) for o in keys)]
    extra = [w for key in keys for w in _synonyms()[key]]
    return " ".join([query] + extra)


class Index:
    def __init__(self) -> None:
        data = json.loads((config.INDEX_DIR / "docs.json").read_text())
        if data["embed_model"] != config.EMBED_MODEL:
            raise RuntimeError("Index built with a different embedding model; run the sync again.")
        self.docs = data["docs"]
        # Menu vocabulary for typo correction: words from item names and categories.
        vocab = {w for d in self.docs for w in _tokens(d["metadata"].get("name", "") + " "
                                                       + " ".join(d["metadata"].get("categories") or []))
                 if len(w) > 2 and w.isascii() and not w.isdigit()}
        self.vocab = vocab
        self._by_sound = {}
        for w in sorted(vocab):
            self._by_sound.setdefault(_sound(w), w)
        self.vectors = np.load(config.INDEX_DIR / "vectors.npy")
        self.bm25 = BM25Okapi([_tokens(d["text"]) for d in self.docs])

    def corrections(self, query: str) -> dict:
        """Misspelt word -> the menu word it most likely means ({"tiqqa": "tikka", "nugets": "nuggets"}).
        Known words, synonym keys ("teekha" means spicy, not tikka) and generic words are left alone."""
        out = {}
        skip = self.vocab | GENERIC_WORDS | {k.lower() for k in _synonyms()}
        for w in _tokens(query):
            if len(w) < 3 or not w.isascii() or w.isdigit() or w in skip:
                continue
            sound = _sound(w)
            if sound in self._by_sound:
                out[w] = self._by_sound[sound]
                continue
            close = difflib.get_close_matches(sound, list(self._by_sound), n=1, cutoff=TYPO_CUTOFF)
            if close:
                out[w] = self._by_sound[close[0]]
        return out

    def correct(self, query: str) -> str:
        """The query plus the menu words its misspellings most likely mean ("nugets" -> "nugets nuggets")."""
        have = set(_tokens(query))
        return " ".join([query] + [e for e in dict.fromkeys(self.corrections(query).values()) if e not in have])

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
        query = self.correct(query)
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


    def keyword_matches(self, query: str, kind: str, branch_id: Optional[str] = None, k: int = 8) -> List[dict]:
        """Items whose name, category or description contains EVERY distinctive word of the query
        (generic food words ignored). Finds all the items a customer's words could mean, e.g. every tikka
        item for "chicken tikka", even those the ranked search leaves out."""
        all_words = [w for w in _tokens(self.correct(query)) if len(w) > 2 and w not in
                     {"how", "much", "the", "price", "prices", "and", "for", "what", "show", "have", "you"}]
        words = {w for w in all_words if w not in GENERIC_WORDS}
        on_name = False
        if not words:  # only generic words, e.g. "pizza deal": match item NAMES holding all of them
            words, on_name = set(all_words), True
            if len(words) < 2:
                return []
        out = []
        for d in self.docs:
            m = d["metadata"]
            if m["kind"] != kind or (branch_id and kind == "menu" and branch_id not in m["branch_ids"]):
                continue
            if words <= set(_tokens(m.get("name", "") if on_name else d["text"])):
                out.append(dict(m))
        return out[:k * 3]


@lru_cache(maxsize=1)
def index() -> Index:
    return Index()
