"""Check a real login token from the main app against the chatbot's JWT settings.

  make check-token TOKEN=eyJhbGciOi...
  (or: .venv/bin/python scripts/check_token.py <token>)

Verifies the signature with JWT_SECRET / JWT_PUBLIC_KEY / JWT_JWKS_URL from .env, lists the claims (values
partly masked), shows which claim the chatbot would use as the customer id, and checks each claim value
against orders.customerId and customers in MongoDB, so you can set JWT_USER_CLAIM with confidence.
Prints nothing secret: not the token, not the key.
"""
import sys
import time
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import jwt  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app import auth, config  # noqa: E402
from app.data.db import client  # noqa: E402


def mask(v) -> str:
    s = str(v)
    return s if len(s) <= 8 else f"{s[:4]}…{s[-4:]} ({len(s)} chars)"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    token = sys.argv[1].strip().removeprefix("Bearer ").strip()
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(token, options={"verify_signature": False})
    print(f"header: alg={header.get('alg')} kid={header.get('kid')}")
    print("claims:")
    for k, v in claims.items():
        shown = (time.strftime("%Y-%m-%d %H:%M", time.localtime(v)) if k in ("exp", "iat", "nbf")
                 and isinstance(v, (int, float)) else (v if isinstance(v, dict) else mask(v)))
        print(f"  {k}: {shown}")

    if not auth.auth_configured():
        print("\nNo JWT_SECRET / JWT_PUBLIC_KEY / JWT_JWKS_URL in .env: signature NOT checked.")
    else:
        try:
            user_id = auth.verify_token(token)
            print(f"\nsignature OK -> chatbot customer id = {mask(user_id)} "
                  f"(from the first present of {config.JWT_USER_CLAIMS})")
        except HTTPException as e:
            print(f"\nREJECTED: {e.detail}" + ("  (check JWT_SECRET matches the Express backend)"
                                               if e.detail == "invalid_token" else ""))

    candidates = {k: v for k, v in claims.items() if isinstance(v, (str, int)) and k not in ("exp", "iat", "nbf")}
    for k, v in claims.items():  # one level of nesting, e.g. {"user": {"id": ...}}
        if isinstance(v, dict):
            candidates.update({f"{k}.{kk}": vv for kk, vv in v.items() if isinstance(vv, (str, int))})
    print("\nwhich claim matches the database?")
    try:
        db = client()
        orders, customers = db["order"]["orders"], db["customer"]["customers"]
        for k, v in candidates.items():
            n_orders = orders.count_documents({"customerId": str(v)}, maxTimeMS=5000)
            n_cust = customers.count_documents({"$or": [{"_id": str(v)}, {"crm_customer_id": str(v)}]}, maxTimeMS=5000)
            flag = "  <-- use this" if n_orders else ""
            print(f"  {k:24} orders.customerId: {n_orders:4}   customers: {n_cust}{flag}")
    except Exception as e:  # noqa: BLE001
        print(f"  could not reach MongoDB ({type(e).__name__}); rerun when Atlas is reachable")


if __name__ == "__main__":
    main()
