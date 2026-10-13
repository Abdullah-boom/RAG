"""All database reads, one function per need. Only this module knows the Mongo schema.

Schema notes (from docs/data_audit.md):
- product.products: name, description, categoryIds (ObjectId[]), isDeal, applicableTo.
- product.productassociations: per-branch price/availability. productId is a *string*, branchId an ObjectId.
  options[] with title 'Variation'/'Size' hold absolute size prices; other options are choices (extra charge).
- order.orders: customerId (string), branchId (string), orderNo, status, orderItems, totals.
"""
import re
import time
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from bson import ObjectId
from bson.errors import InvalidId

from app.data.db import QUERY_TIMEOUT_MS, faq_collection, order_db, product_db

PK_TZ = ZoneInfo("Asia/Karachi")
SIZE_OPTION = re.compile(r"^\s*(variation|vriation|size|select size)\s*$", re.I)
TEST_NAME = re.compile(r"\btest", re.I)
JUNK_CATEGORY = re.compile(r"test|^uuu$|burgerrrrr|^pizza$", re.I)
CURRENCY = "PKR"


def _oid(value) -> Optional[ObjectId]:
    try:
        return ObjectId(str(value))
    except (InvalidId, TypeError):
        return None


def _clean(name: str) -> str:
    return (name or "").strip().strip("'").strip()


# ---------- branches ----------

def list_branches(active_only: bool = True) -> List[dict]:
    q = {"status": "ACTIVE", "isVisible": {"$ne": False}} if active_only else {}
    rows = product_db().branches.find(q, {"name": 1, "city": 1}).max_time_ms(QUERY_TIMEOUT_MS)
    return [{"branch_id": str(b["_id"]), "name": _clean(b["name"]), "city": b.get("city")} for b in rows]


def _parse_clock(s: str):
    return datetime.strptime(s.strip().lower(), "%I:%M%p").time()


def get_branch_status(branch_id: str, now: Optional[datetime] = None) -> Optional[dict]:
    b = product_db().branches.find_one(
        {"_id": _oid(branch_id)},
        {"name": 1, "city": 1, "status": 1, "isAvailable": 1, "inactiveReason": 1,
         "branchTiming": 1, "facilities": 1, "minOrderAmount": 1},
        max_time_ms=QUERY_TIMEOUT_MS,
    )
    if not b:
        return None
    now = now or datetime.now(PK_TZ)
    day = now.strftime("%A").upper()
    hours, open_now = [], False
    for d in b.get("branchTiming") or []:
        if d.get("day") != day:
            continue
        for t in d.get("timing") or []:
            hours.append(f"{t['startTime']}-{t['endTime']}")
            try:
                start, end = _parse_clock(t["startTime"]), _parse_clock(t["endTime"])
            except (KeyError, ValueError):
                continue
            cur = now.time()
            open_now |= (start <= cur <= end) if start <= end else (cur >= start or cur <= end)
    accepting = b.get("status") == "ACTIVE" and bool(b.get("isAvailable"))
    return {
        "branch_id": str(b["_id"]),
        "branch": _clean(b["name"]),
        "city": b.get("city"),
        "open_now": bool(accepting and open_now),
        "accepting_orders": accepting,
        "closed_reason": None if accepting else (b.get("inactiveReason") or None),
        "today_hours": hours,
        "services": b.get("facilities") or [],
        "min_order_amount": b.get("minOrderAmount"),
    }


# ---------- menu (live facts) ----------

def _split_options(options: list):
    sizes, choices = [], []
    for o in options or []:
        vars_ = [v for v in o.get("variations") or [] if v.get("isAvailable", True)]
        if SIZE_OPTION.match(o.get("title") or ""):
            sizes += [{"size": _clean(v["name"]), "price": v.get("price")} for v in vars_]
        else:
            choices.append({
                "choice": _clean(o.get("title")),
                "required": bool(o.get("isRequired")),
                "options": [
                    _clean(v["name"]) + (f" (+{v['price']} {CURRENCY})" if v.get("price") else "")
                    for v in vars_
                ][:12],
            })
    return sizes, choices


_CATEGORY_CACHE: Dict = {"at": 0.0, "names": {}}


def _category_names() -> Dict[ObjectId, str]:
    if time.time() - _CATEGORY_CACHE["at"] > 120:  # short TTL cache (spec 8)
        _CATEGORY_CACHE["names"] = {c["_id"]: _clean(c["name"]) for c in product_db().categories.find({}, {"name": 1})}
        _CATEGORY_CACHE["at"] = time.time()
    return _CATEGORY_CACHE["names"]


def get_items(product_ids: List[str], branch_id: str, detailed: bool = False) -> List[dict]:
    """Live price/availability for products at one branch, in the order given."""
    oids = [o for o in map(_oid, product_ids) if o]
    products = {
        str(p["_id"]): p
        for p in product_db().products.find(
            {"_id": {"$in": oids}}, {"name": 1, "description": 1, "isDeal": 1, "image": 1, "categoryIds": 1}
        ).max_time_ms(QUERY_TIMEOUT_MS)
    }
    assocs = {
        a["productId"]: a
        for a in product_db().productassociations.find(
            {"productId": {"$in": list(products)}, "branchId": _oid(branch_id), "isDeleted": False}
        ).max_time_ms(QUERY_TIMEOUT_MS)
    }
    cats = _category_names()
    out = []
    for pid in product_ids:
        p = products.get(pid)
        if not p:
            continue
        a = assocs.get(pid)
        item = {
            "item_id": pid,
            "name": _clean(p["name"]),
            "category": ", ".join(c for c in (cats.get(x) for x in p.get("categoryIds") or [])
                                  if c and not JUNK_CATEGORY.search(c)),
            "description": (p.get("description") or "").strip()[: 300 if detailed else 90],
            "is_deal": bool(p.get("isDeal")),
            "sold_at_this_branch": a is not None,
            "available": bool(a and a.get("isAvailable")),
            "price": a.get("price") if a else None,
            "currency": CURRENCY,
            "image_url": p.get("image"),
        }
        if a:
            sizes, choices = _split_options(a.get("options"))
            if sizes:
                # Base price can disagree with every size price in this DB; show only size prices.
                item["price"] = None
                item["sizes"] = sizes
                item["price_from"] = min(s["price"] for s in sizes if s["price"] is not None)
            if detailed and choices:
                item["choices"] = choices
        out.append(item)
    return out


def get_deal_ids(branch_id: str) -> List[str]:
    deal_ids = [str(p["_id"]) for p in product_db().products.find({"isDeal": True}, {"_id": 1})]
    rows = product_db().productassociations.find(
        {"productId": {"$in": deal_ids}, "branchId": _oid(branch_id), "isDeleted": False, "isAvailable": True},
        {"productId": 1},
    ).max_time_ms(QUERY_TIMEOUT_MS)
    return [r["productId"] for r in rows]


def get_best_seller_ids(branch_id: str) -> List[str]:
    """Items Cheezious admins flagged as best sellers: branch-level flags first, then global product flags.
    (Sales counts are not usable: products.orderdedTimes is always 0 and dev orders are mostly test data.)"""
    branch = _oid(branch_id)
    sold_here = {a["productId"]: a for a in product_db().productassociations.find(
        {"branchId": branch, "isDeleted": False, "isAvailable": True}, {"productId": 1, "isBestSeller": 1}
    ).max_time_ms(QUERY_TIMEOUT_MS)}
    branch_flagged = [pid for pid, a in sold_here.items() if a.get("isBestSeller")]
    global_flagged = [str(p["_id"]) for p in product_db().products.find({"isBestSeller": True}, {"_id": 1})
                      if str(p["_id"]) in sold_here and str(p["_id"]) not in branch_flagged]
    return branch_flagged + global_flagged


def get_active_discounts(branch_id: str) -> List[dict]:
    now = datetime.utcnow()
    rows = product_db().discounts.find(
        {"isAvailable": True, "branchIds": {"$in": [branch_id, _oid(branch_id)]},
         "startDate": {"$lte": now}, "endDate": {"$gte": now}},
        {"type": 1, "applicableOn": 1, "amount": 1, "percentage": 1, "maxDiscountAmount": 1,
         "minOrderAmount": 1, "endDate": 1},
    ).max_time_ms(QUERY_TIMEOUT_MS)
    return [{
        "type": d["type"], "applies_to": d.get("applicableOn"), "flat_amount": d.get("amount"),
        "percentage": d.get("percentage"), "max_discount": d.get("maxDiscountAmount"),
        "min_order_amount": d.get("minOrderAmount"), "valid_until": d["endDate"].isoformat() + "Z",
    } for d in rows]


# ---------- ingestion source ----------

def menu_rows_for_index() -> List[dict]:
    """One row per sellable product, joined with categories and per-branch prices (for the vector index)."""
    db = product_db()
    cats = {c["_id"]: _clean(c["name"]) for c in db.categories.find({}, {"name": 1})}
    agg: Dict[str, dict] = {}
    for a in db.productassociations.find(
        {"isDeleted": False},
        {"productId": 1, "branchId": 1, "price": 1, "isAvailable": 1, "options.title": 1,
         "options.variations.name": 1, "options.variations.price": 1},
    ):
        r = agg.setdefault(a["productId"], {"branch_ids": set(), "prices": [], "sizes": set()})
        if a.get("isAvailable"):
            r["branch_ids"].add(str(a["branchId"]))
        if a.get("price"):
            r["prices"].append(a["price"])
        for o in a.get("options") or []:
            if SIZE_OPTION.match(o.get("title") or ""):
                for v in o.get("variations") or []:
                    r["sizes"].add(_clean(v.get("name")))
                    if v.get("price"):
                        r["prices"].append(v["price"])
    rows = []
    for p in db.products.find({}, {"name": 1, "description": 1, "categoryIds": 1, "isDeal": 1, "updatedAt": 1}):
        pid = str(p["_id"])
        categories = [cats[c] for c in p.get("categoryIds") or [] if c in cats]
        if (categories and all(JUNK_CATEGORY.search(c) for c in categories)) or TEST_NAME.search(p["name"] or ""):
            continue  # test data
        a = agg.get(pid)
        if not a or not a["branch_ids"]:
            continue  # not sold anywhere
        rows.append({
            "item_id": pid,
            "name": _clean(p["name"]),
            "description": (p.get("description") or "").strip(),
            "categories": [c for c in categories if not JUNK_CATEGORY.search(c)],
            "is_deal": bool(p.get("isDeal")),
            "branch_ids": sorted(a["branch_ids"]),
            "price_min": min(a["prices"]) if a["prices"] else None,
            "price_max": max(a["prices"]) if a["prices"] else None,
            "sizes": sorted(s for s in a["sizes"] if s),
            "source_updated_at": p.get("updatedAt").isoformat() + "Z" if p.get("updatedAt") else None,
        })
    return rows


def faq_rows_for_index() -> List[dict]:
    rows = faq_collection().find({"type": "CUSTOMER", "status": "PUBLISH"}, {"question": 1, "answer": 1, "updatedAt": 1})
    return [{"faq_id": str(f["_id"]), "question": f["question"].strip(), "answer": f["answer"].strip(),
             "source_updated_at": f["updatedAt"].isoformat() + "Z" if f.get("updatedAt") else None} for f in rows]


# ---------- customer-private (always scoped by user_id from server context) ----------

def _order_summary(o: dict, branch_names: Dict[str, str], detailed: bool) -> dict:
    s = {
        "order_no": o.get("orderNo"),
        "status": o.get("status"),
        "order_type": o.get("type"),
        "placed_at": o["createdAt"].isoformat() + "Z" if o.get("createdAt") else None,
        "branch": branch_names.get(o.get("branchId")),
        "total": o.get("total"),
        "currency": CURRENCY,
    }
    if detailed:
        s.update({
            "subtotal": o.get("subTotal"),
            "tax": o.get("taxAmount"),
            "discount": o.get("total_discount_amount") or 0,
            "payment_type": o.get("paymentType"),
            "delivery_eta_minutes": o.get("orderDeliveryETA"),
            "delivered_at": o["deliveredAt"].isoformat() + "Z" if o.get("deliveredAt") else None,
            "items": [{
                "name": _clean(i.get("productName")),
                "qty": i.get("productQuantity"),
                "options": [_clean(v.get("name")) for po in i.get("productOptions") or [] for v in po.get("variations") or []],
            } for i in (o.get("orderItems") or [])[:15]],
        })
    else:
        s["item_count"] = sum(i.get("productQuantity") or 1 for i in o.get("orderItems") or [])
    return s


_ORDER_FIELDS = {"orderNo": 1, "status": 1, "type": 1, "createdAt": 1, "branchId": 1, "total": 1, "subTotal": 1,
                 "taxAmount": 1, "total_discount_amount": 1, "paymentType": 1, "orderDeliveryETA": 1,
                 "deliveredAt": 1, "orderItems.productName": 1, "orderItems.productQuantity": 1,
                 "orderItems.productOptions.variations.name": 1}


def _branch_names(orders: List[dict]) -> Dict[str, str]:
    ids = [o for o in {_oid(x.get("branchId")) for x in orders} if o]
    return {str(b["_id"]): _clean(b["name"]) for b in product_db().branches.find({"_id": {"$in": ids}}, {"name": 1})}


def get_order(user_id: str, order_no: Optional[str] = None) -> Optional[dict]:
    if not user_id:
        return None
    q = {"customerId": user_id}  # identity filter: never removed, never LLM-supplied
    if order_no:
        q["orderNo"] = order_no.strip()
    o = order_db().orders.find_one(q, _ORDER_FIELDS, sort=[("createdAt", -1)], max_time_ms=QUERY_TIMEOUT_MS)
    return _order_summary(o, _branch_names([o]), detailed=True) if o else None


def get_order_history(user_id: str, limit: int = 5) -> List[dict]:
    if not user_id:
        return []
    rows = list(order_db().orders.find({"customerId": user_id}, _ORDER_FIELDS)
                .sort("createdAt", -1).limit(min(limit, 10)).max_time_ms(QUERY_TIMEOUT_MS))
    names = _branch_names(rows)
    return [_order_summary(o, names, detailed=False) for o in rows]
