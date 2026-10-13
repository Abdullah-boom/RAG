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

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_FALLBACK_MODELS = [m for m in os.getenv("GROQ_FALLBACK_MODELS", "openai/gpt-oss-20b").split(",") if m]
MONGO_URI = os.getenv("MONGO_URI", "")
EMBED_MODEL = os.getenv("EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
INDEX_DIR = ROOT / "index"

TEMPERATURE = 0.1
MAX_TOOL_CALLS = 4
HISTORY_TURNS = 6

# Website API (api.py)
REDIS_URL = os.getenv("REDIS_URL", "")  # empty = in-process memory (dev only: lost on restart, one worker)
SESSION_TTL_MIN = int(os.getenv("SESSION_TTL_MIN", "1440"))
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()]
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "20"))
TRUST_PROXY = os.getenv("TRUST_PROXY", "false").lower() == "true"  # read client IP from X-Forwarded-For
API_DEBUG = os.getenv("API_DEBUG", "false").lower() == "true"  # include tools/latency in API responses
