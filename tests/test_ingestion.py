from app.ingestion.build_documents import build_menu_document

ROW = {"item_id": "abc", "name": "Chicken Tikka", "description": "Grilled chicken", "categories": ["Somewhat Local"],
       "is_deal": False, "branch_ids": ["b1"], "price_min": 690, "price_max": 3000, "sizes": ["Small", "Large"]}


def test_menu_document_shape():
    d = build_menu_document(ROW)
    assert d["id"] == "menu:abc"  # deterministic -> idempotent
    assert "Chicken Tikka" in d["text"] and "Small, Large" in d["text"]
    assert "690" not in d["text"]  # prices are metadata filters only, never embedded text
    assert d["metadata"]["branch_ids"] == ["b1"]


def test_no_personal_data_in_index():
    import json
    from app import config
    text = (config.INDEX_DIR / "docs.json").read_text()
    for marker in ("customerId", "phoneNo", "customerName", "orderNo", "address"):
        assert marker not in text
    assert all(d["metadata"]["kind"] in ("menu", "faq") for d in json.loads(text)["docs"])
