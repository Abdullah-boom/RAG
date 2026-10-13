"""DB row -> text (embedded) + metadata (filters only). Spec 7.3 / Appendix B.1."""
from app import config


def build_menu_document(row: dict) -> dict:
    text = (
        f"Item: {row['name']}\n"
        f"Category: {', '.join(row['categories']) or 'not listed'}\n"
        f"Type: {'deal / combo' if row['is_deal'] else 'single item'}\n"
        f"Description: {row.get('description') or ''}\n"
        f"Sizes: {', '.join(row.get('sizes') or []) or 'single'}"
    )
    return {
        "id": f"menu:{row['item_id']}",  # deterministic id -> idempotent rebuilds
        "text": text,
        "metadata": {
            "kind": "menu",
            "item_id": row["item_id"],
            "name": row["name"],
            "categories": row["categories"],
            "is_deal": row["is_deal"],
            "branch_ids": row["branch_ids"],
            "price_min": row["price_min"],  # coarse filter only; never shown to customers
            "price_max": row["price_max"],
            "source_updated_at": row.get("source_updated_at"),
            "embed_model": config.EMBED_MODEL,
        },
    }


def build_faq_document(row: dict) -> dict:
    return {
        "id": f"faq:{row['faq_id']}",
        "text": f"Q: {row['question']}\nA: {row['answer']}",
        "metadata": {"kind": "faq", "faq_id": row["faq_id"], "question": row["question"],
                     "answer": row["answer"], "source": "aggregator.faqs",
                     "source_updated_at": row.get("source_updated_at"), "embed_model": config.EMBED_MODEL},
    }
