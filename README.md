# Cheezious Assistant: Phase 1 test drive

Read-only RAG chatbot over the dev MongoDB, built from the spec in [docs/Fast_Food_RAG_Chatbot_Build_Spec.pdf](docs/Fast_Food_RAG_Chatbot_Build_Spec.pdf).
English, Roman Urdu and Urdu script. Free stack: Groq (gpt-oss), local multilingual embeddings, BM25.

## Run

Always use the project venv (Python 3.12). Plain `python3` on this Mac is the system 3.9 without these packages.

```bash
make setup     # once: .venv on Python 3.12 (via uv) + pinned packages
make index     # build the search index from MongoDB (re-run after menu changes)
make ui        # Streamlit chat UI -> http://localhost:8501
make chat      # terminal chat
make test      # unit/security tests
make api       # website API on http://localhost:8000 (serves the built widget at /widget/)
make widget    # build the React chat widget into widget/dist
make widget-dev  # widget dev server on http://localhost:5173 (with `make api` running)
```

Without make: `source .venv/bin/activate`, then `streamlit run ui.py`. VS Code picks `.venv` automatically (`.vscode/settings.json`).
`.env` needs `GROQ_API_KEY` and `MONGO_URI` (see `.env.example`). Its values override anything exported in your shell.

## Website chat widget

The website chatbot is a React widget (`widget/`) that talks to a FastAPI backend (`app/api.py`).
Streamlit (`ui.py`) stays as the internal test UI.

**Embed on any website** (React, WordPress, plain HTML) with one tag before `</body>`:

```html
<script src="https://API_HOST/widget/cheezious-chat.js" defer></script>
```

Optional attributes: `data-api-url` (defaults to the script's own host), `data-title`, `data-color` (accent, e.g. `#f59f00`),
`data-position` (`right` | `left`). The page can open it with `window.CheeziousChat.open()`.
The widget renders inside a Shadow DOM, so the site's CSS and the widget's CSS can't affect each other.
Add the website's origin to `CORS_ORIGINS` in `.env`. Try it locally at http://localhost:8000/widget/demo.html.

**API** (guest only for now; the customer picks a branch in the widget):

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/branches` | Active branches (test branches hidden) |
| GET | `/api/branches/{id}/status` | Open now, today's hours |
| POST | `/api/chat` | `{message, branch_id, session_id?}` -> answer, item cards, suggestions, cart, `session_id` |
| GET | `/api/session?branch_id&session_id` | Restore a conversation after a page reload |
| POST | `/api/cart/clear` | `{branch_id, session_id}` |

The server mints `session_id`; conversations are keyed by branch + session, so switching branch starts fresh.
User identity is never read from the request: when the website has login, verify its JWT in `app/api.py`.
Rate limit: `RATE_LIMIT_PER_MIN` messages per IP (in-process, per worker).

**Deploy with Docker** (API + Redis for chat memory):

```bash
cp .env.example .env                                   # fill GROQ_API_KEY, MONGO_URI, CORS_ORIGINS
docker compose run --rm api python -m app.ingestion.sync   # build the search index into ./index
docker compose up -d --build                          # http://localhost:8000
```

Put it behind HTTPS (nginx, Caddy or a cloud load balancer) and set `TRUST_PROXY=true` there so rate limits
see real client IPs. Re-run the sync command after menu changes.

## How it follows the spec

| Spec | Here |
|---|---|
| Golden rule (s2) | Search only finds item ids; prices, availability, deals, timings and orders are read live from Mongo by id |
| Identity (s9, s13) | `user_id` and `branch_id` come from `RequestContext` (server side). Identity args sent by the model are dropped; unknown args are rejected |
| Retrieval (s8) | Dense (multilingual MiniLM) + BM25, RRF fusion, exact-name boost, branch/price/deal filters, synonyms incl. Roman Urdu and Urdu, "no result" threshold |
| Agent (s5, s10) | LangGraph `StateGraph`: pre_checks -> agent <-> tools -> post_checks (1 retry) -> format_response; memory per thread via `InMemorySaver`, tool outputs dropped from memory after each turn |
| Guards (s10) | Price guard (every Rs amount must appear in this turn's tool results), grounding guard, 4 tool calls max, 1 retry, safe fallback, offline keyword fallback |
| Never embedded (s7.5) | Index holds menu items and published customer FAQs only (tested) |
| Tracing (s13) | `logs/traces.jsonl` with phone/email redaction (stand-in for Langfuse) |

## Deviations (test drive)

- **Groq instead of Ollama.** Prompts, including order summaries, go to Groq. Order tools return no names, phones or addresses, but get a privacy review before using real customer data.
- **No Qdrant or real login yet.** The index is files in `index/`. Chat memory is in Redis under Docker (in-process when `REDIS_URL` is empty). The website widget is guest-only; Streamlit and the CLI simulate login (sidebar Customer ID / `--user`).
- **Groq free tier:** 8k tokens/min per model. Falls back from `gpt-oss-120b` to `gpt-oss-20b`, then waits briefly.

## Data notes (from `docs/data_audit.md`)

- Source DBs: `product` (menu, per-branch prices in `productassociations`), `order.orders`, `aggregator.faqs`.
- An item's base `price` can disagree with its size prices (e.g. Stuff Crust: base 1600, sizes 1450/2090). When sizes exist, only size prices are shown. **Need confirmation of which the app charges.**
- No allergen or spice data in the DB, so the bot says so.
- Many test categories, branches and products are in the dev data. Categories and names containing "test" are filtered out of the index.
- Only 3 published customer FAQs; no delivery or refund policy text.
