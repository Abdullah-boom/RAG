"""Settings read from .env (spec section 15)."""
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # running with the system Python instead of the project venv
    sys.exit("Missing packages: run with the project venv, e.g. `make ui` or `.venv/bin/python ...` "
             "(or `source .venv/bin/activate` first).")

ROOT = Path(__file__).resolve().parent.parent
# override=True: values in .env win over stale variables exported in the shell.
load_dotenv(ROOT / ".env", override=True)

# LLM: "ollama" = local model (no API key, no rate limits), "groq" = Groq cloud
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
# Seconds one chat turn may spend on the LLM before falling back. Raise it for Ollama on a CPU-only machine.
LLM_TIME_BUDGET_S = float(os.getenv("LLM_TIME_BUDGET_S", "20"))
# Optional "user:password" when Ollama sits behind a tunnel with basic auth (e.g. ngrok on Colab).
OLLAMA_AUTH = os.getenv("OLLAMA_AUTH", "")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_FALLBACK_MODELS = [m for m in os.getenv("GROQ_FALLBACK_MODELS", "openai/gpt-oss-20b").split(",") if m]
MONGO_URI = os.getenv("MONGO_URI", "")
EMBED_MODEL = os.getenv("EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
INDEX_DIR = ROOT / "index"

TEMPERATURE = 0.1
MAX_TOOL_CALLS = 4
HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "6"))  # past exchanges sent to the LLM; small local models do better with 1-2
# ...and at most this many characters of them (~4 chars a token), newest kept: one long chat can't blow the budget.
HISTORY_MAX_CHARS = int(os.getenv("HISTORY_MAX_CHARS", "6000"))

# Website API (api.py)
REDIS_URL = os.getenv("REDIS_URL", "")  # empty = in-process memory (dev only: lost on restart, one worker)
SESSION_TTL_MIN = int(os.getenv("SESSION_TTL_MIN", "1440"))
# Carts are kept per customer and branch, apart from chats; an untouched cart expires after this (Redis only).
CART_TTL_DAYS = int(os.getenv("CART_TTL_DAYS", "30"))

# Order hand-off (app/checkout.py): the main backend endpoint that places orders. Empty = not connected yet,
# and a confirmed cart is completed in the app instead. The bot never places orders or takes payment itself.
ORDER_BACKEND_URL = os.getenv("ORDER_BACKEND_URL", "")
ORDER_BACKEND_KEY = os.getenv("ORDER_BACKEND_KEY", "")  # sent as Authorization: Bearer, if the backend wants one
CHECKOUT_SECRET = os.getenv("CHECKOUT_SECRET", "")  # signs confirm tokens; defaults to JWT_SECRET
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()]
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "20"))
TRUST_PROXY = os.getenv("TRUST_PROXY", "false").lower() == "true"  # read client IP from X-Forwarded-For
API_DEBUG = os.getenv("API_DEBUG", "false").lower() == "true"  # include tools/latency in API responses

# Login (auth.py): verify the main app's JWT. Configure one of secret / public key / JWKS URL.
JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_PUBLIC_KEY = os.getenv("JWT_PUBLIC_KEY", "").replace("\\n", "\n")  # PEM; \n escapes allowed in .env
JWT_JWKS_URL = os.getenv("JWT_JWKS_URL", "")
JWT_ALGORITHMS = [a.strip() for a in os.getenv("JWT_ALGORITHMS", "").split(",") if a.strip()]
# Claim(s) holding the customer id; the first one present is used (dotted paths like user.id work).
JWT_USER_CLAIMS = [c.strip() for c in os.getenv("JWT_USER_CLAIM", "sub,id,_id,userId,customerId").split(",")
                   if c.strip()]
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "")
JWT_ISSUER = os.getenv("JWT_ISSUER", "")
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "false").lower() == "true"

# Local testing only: /api/dev/* lets the demo page log in as any customer (signs a JWT with JWT_SECRET).
# Never enable on a server customers can reach.
DEV_LOGIN = os.getenv("DEV_LOGIN", "false").lower() == "true"
