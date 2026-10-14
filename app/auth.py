"""Verifies the login JWT issued by the main (MERN) app and returns the customer id (spec 12, 13).

Configure ONE of these in .env, matching how the Express backend signs its tokens:
  JWT_SECRET       shared secret (HS256 by default)
  JWT_PUBLIC_KEY   PEM public key (RS256 by default; set JWT_ALGORITHMS=ES256 for EC keys)
  JWT_JWKS_URL     JWKS endpoint (key chosen by the token's `kid`)
JWT_USER_CLAIM lists the claim(s) holding the customer id, first present wins (default
`sub,id,_id,userId,customerId`; dotted paths like `user.id` work). scripts/check_token.py shows which one a
real token carries and whether it matches orders.customerId.
Optional: JWT_AUDIENCE, JWT_ISSUER. AUTH_REQUIRED=true rejects guests.

No Authorization header -> guest (None). A token that fails verification -> 401, never a silent guest,
so an expired login is noticed instead of quietly losing order access. With no verifier configured,
tokens are ignored and everyone is a guest (safe: guests can't read orders).
"""
import logging
from functools import lru_cache
from typing import Optional

import jwt
from fastapi import HTTPException, Request

from app import config

log = logging.getLogger("chatbot.auth")
MAX_USER_ID_CHARS = 64


def auth_configured() -> bool:
    return bool(config.JWT_SECRET or config.JWT_PUBLIC_KEY or config.JWT_JWKS_URL)


@lru_cache(maxsize=1)
def _jwks_client() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(config.JWT_JWKS_URL, cache_keys=True, lifespan=3600)


def _algorithms() -> list:
    if config.JWT_ALGORITHMS:
        return config.JWT_ALGORITHMS
    return ["HS256"] if config.JWT_SECRET else ["RS256"]


def _key(token: str):
    if config.JWT_SECRET:
        return config.JWT_SECRET
    if config.JWT_PUBLIC_KEY:
        return config.JWT_PUBLIC_KEY
    return _jwks_client().get_signing_key_from_jwt(token).key


def _claim(payload: dict, path: str):
    value = payload
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def verify_token(token: str) -> str:
    """Return the customer id from a valid token; raise HTTPException(401) otherwise."""
    try:
        payload = jwt.decode(
            token, _key(token), algorithms=_algorithms(),
            audience=config.JWT_AUDIENCE or None, issuer=config.JWT_ISSUER or None,
            options={"require": ["exp"], "verify_aud": bool(config.JWT_AUDIENCE)},
            leeway=30,
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "token_expired")
    except (jwt.PyJWTError, ValueError) as e:
        log.info("rejected token: %s", type(e).__name__)
        raise HTTPException(401, "invalid_token")
    user_id = next((v for v in (_claim(payload, c) for c in config.JWT_USER_CLAIMS) if v not in (None, "")), None)
    if not isinstance(user_id, (str, int)) or not str(user_id) or len(str(user_id)) > MAX_USER_ID_CHARS:
        raise HTTPException(401, "invalid_token")
    return str(user_id)


def user_from_request(request: Request) -> Optional[str]:
    """Customer id from `Authorization: Bearer <jwt>`, or None for a guest."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if not header:
        if config.AUTH_REQUIRED:
            raise HTTPException(401, "login_required")
        return None
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(401, "invalid_token")
    if not auth_configured():
        return None  # login not wired up yet: treat as guest rather than trusting an unverified token
    return verify_token(token.strip())
