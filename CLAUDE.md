# Cheezious Assistant

Read-only RAG chatbot over the dev MongoDB (menu, FAQs, branches, orders) for a fast-food chain.
English, Roman Urdu and Urdu script. README.md is the full reference (API table, widget embedding, auth).

## Layout

- `app/api.py`: FastAPI backend for the website widget. Its module docstring explains identity, carts, payment and checkout.
- `app/agent/`: LangGraph agent (`graph.py`), guards, intents, modular prompts in `prompts/`.
- `app/retrieval.py`, `app/ingestion/`: hybrid search (local multilingual embeddings + BM25); index lives in `index/` (gitignored).
- `app/data/`: MongoDB access (read-only).
- `widget/`: React 18 + TypeScript + Vite chat widget, built to `widget/dist/cheezious-chat.js`.
- `ui.py`: Streamlit internal test UI. `chat.py`: terminal chat.
- `tests/`, `eval/`: pytest suite and golden-set evaluation.

## Running locally

Always use `.venv` (Python 3.12); the system `python3` lacks the packages. Settings come from `.env`
(copy `.env.example`); `.env` values override the shell.

```bash
open -a Ollama   # LLM_PROVIDER=ollama needs the Ollama server on :11434
make index       # rebuild search index from MongoDB (after menu changes, or if index/ is missing)
make api         # API + built widget: http://localhost:8000/widget/demo.html
make ui          # Streamlit: http://localhost:8501
make widget      # rebuild widget/dist after editing widget/src
make test        # 255 tests; Mongo-backed ones fail without a reachable MONGO_URI
```

- The API mounts `widget/dist` only at startup: after the first `make widget`, restart `make api`
  (`--reload` watches Python files only).
- Smoke-test the API with a real chat: `POST /api/chat` with `branch_id` from `GET /api/branches`.
  `X-Guest-Id` and `session_id` must be server-style ids (32-char hex, e.g. `uuid4().hex`), or they're ignored.
- Small local models: keep `HISTORY_TURNS` low and raise `LLM_TIME_BUDGET_S` on CPU-only machines.
