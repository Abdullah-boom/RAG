"""Customer location -> default branch, from the branches' delivery areas (product.polygons).

Pure decision logic: the polygon lookup (repo.branches_covering) and the branch list are passed in, so
every outcome is testable without a database. Never guesses: if no open branch delivers to the spot, the
answer has no branch and the app asks the customer to pick one.
"""
import math
from datetime import datetime
from typing import List, Optional

from app.data import repositories as repo

# Outcome codes the apps switch on.
MATCHED = "matched"              # an open branch delivers here: use it as the default
CLOSED = "branch_closed"         # branch(es) deliver here but none is open now: ask the customer to pick
NO_COVERAGE = "no_coverage"      # no active branch delivers here: ask the customer to pick


def _distance_km(lat: float, lng: float, location: Optional[list]) -> float:
    """Great-circle distance to a branch's GeoJSON point ([lng, lat]); unknown location sorts last."""
    if not location or len(location) != 2:
        return math.inf
    lng2, lat2 = location
    p1, p2 = math.radians(lat), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def choose_branch(lat: float, lng: float, covering_ids: List[str], branches: List[dict],
                  now: Optional[datetime] = None) -> dict:
    """covering_ids: branches whose delivery area contains the point. branches: the selectable list
    (repo.list_branches rows). Where areas overlap, an open branch beats a closed one, then the nearest wins."""
    by_id = {b["branch_id"]: b for b in branches}
    candidates = [by_id[i] for i in covering_ids if i in by_id]  # inactive / hidden / test branches drop out
    if not candidates:
        return {"reason": NO_COVERAGE, "branch_id": None, "covering": []}
    candidates.sort(key=lambda b: (not repo.open_now(b, now), _distance_km(lat, lng, b.get("location"))))
    best = candidates[0]
    covering = [b["branch_id"] for b in candidates]
    if not repo.open_now(best, now):
        return {"reason": CLOSED, "branch_id": None, "covering": covering}
    return {"reason": MATCHED, "branch_id": best["branch_id"], "covering": covering}
