"""Full rebuild of the local search index from MongoDB (spec 7.4). Re-runnable: it overwrites.

Usage: python -m app.ingestion.sync
"""
import json
import sys
import time

import numpy as np

from app import config
from app.data import repositories as repo
from app.ingestion.build_documents import build_faq_document, build_menu_document
from app.retrieval import embed


def main() -> None:
    t0 = time.time()
    menu_rows = repo.menu_rows_for_index()
    faq_rows = repo.faq_rows_for_index()
    docs = [build_menu_document(r) for r in menu_rows] + [build_faq_document(r) for r in faq_rows]
    if not docs:
        sys.exit("No documents built; check DB access.")
    vectors = embed([d["text"] for d in docs])
    config.INDEX_DIR.mkdir(exist_ok=True)
    tmp_vec, tmp_docs = config.INDEX_DIR / "vectors.tmp.npy", config.INDEX_DIR / "docs.tmp.json"
    np.save(tmp_vec, vectors)
    tmp_docs.write_text(json.dumps({"embed_model": config.EMBED_MODEL, "docs": docs}, ensure_ascii=False))
    tmp_vec.replace(config.INDEX_DIR / "vectors.npy")  # swap in only after both files are written
    tmp_docs.replace(config.INDEX_DIR / "docs.json")
    print(f"Indexed {len(menu_rows)} menu items + {len(faq_rows)} FAQs in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
