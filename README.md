# Cheezious Assistant: Phase 1 test drive

Read-only RAG chatbot over the dev MongoDB, built from the spec in [docs/Fast_Food_RAG_Chatbot_Build_Spec.pdf](docs/Fast_Food_RAG_Chatbot_Build_Spec.pdf).
English, Roman Urdu and Urdu script. Free stack: local LLM via Ollama (`qwen2.5:1.5b`, Groq optional), local multilingual embeddings, BM25.

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
`.env` needs `MONGO_URI` (see `.env.example`). The LLM is Ollama by default: install it and `ollama pull qwen2.5:1.5b`
(or set `LLM_PROVIDER=groq` and `GROQ_API_KEY`). Its values override anything exported in your shell.

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

**Login (so order questions work).** In the host app (e.g. the MERN app's root layout), give the widget the
customer's JWT before the script loads, and clear it on logout:

```js
window.CheeziousChatConfig = { getToken: () => localStorage.getItem("token") };  // however your app stores it
// later: window.CheeziousChat.setAuth(() => newToken)   on logout: window.CheeziousChat.setAuth(null)
```

Then set ONE of `JWT_SECRET` / `JWT_PUBLIC_KEY` / `JWT_JWKS_URL` in `.env` to match how the Express backend
signs tokens, and `JWT_USER_CLAIM` to the claim holding the customer id (must equal `orders.customerId`).
Until then every visitor is a guest. Expired or forged tokens get `401`; the body can never set identity.

**API** (`Authorization: Bearer <jwt>` optional; guests send `X-Guest-Id`, see Carts below):

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/branches` | Active branches with `open_now` (test branches hidden). Only open ones can be selected |
| POST | `/api/branches/locate` | `{lat, lng}` (WGS84 degrees) -> `{reason, branch, covering}`: the default branch for the device's location |
| POST | `/api/branches/select` | `{branch_id}` -> `{branch, status, cart}`. `409 branch_closed` if not open now, `404` if unknown |
| GET | `/api/branches/{id}/status` | Open now, today's hours |
| POST | `/api/chat` | `{message, branch_id, session_id?, stream?}` -> answer, item cards, suggestions, cart, `session_id`. With `stream: true`: Server-Sent Events, `status` lines while the agent works, then one `answer` or `error` |
| GET | `/api/session?branch_id&session_id` | Restore a conversation after a page reload |
| GET | `/api/cart?branch_id` | That branch's cart |
| POST | `/api/cart/add` | `{branch_id, item_id, size?, qty?}` |
| POST | `/api/cart/quantity` | `{branch_id, line_no, qty}` (qty >= 1) |
| POST | `/api/cart/remove` | `{branch_id, line_no}` |
| POST | `/api/cart/clear` | `{branch_id}` |
| GET | `/api/payment-methods?branch_id` | Methods this branch accepts now, cheapest first, each with estimated tax + total for this cart, and the current `chosen` code |
| POST | `/api/cart/payment` | `{branch_id, method}` (a code, e.g. `COD`): choose how to pay for this cart. `409 payment_method_unavailable` if the branch doesn't offer it |
| POST | `/api/checkout/review` | `{branch_id}` -> `{ready, problems[], summary, confirm_token}`: is the cart ready to order |
| POST | `/api/checkout/confirm` | `{branch_id, confirm_token}` (the customer's tap) -> `{status, order_no, message, cart}` |

**Opening the chat (web and mobile).** Each time the chat opens:
1. Get the device location if the customer allows it (browser geolocation / the OS location permission).
2. `POST /api/branches/locate`. The server finds the active, enabled `DELIVERYAREA` polygon in
   `product.polygons` that contains the point (Mongo `$geoIntersects` on the existing 2dsphere index; GeoJSON
   is `[lng, lat]`, the API takes `lat`/`lng` by name). A branch with an active `REDZONE` there doesn't count.
   Where areas overlap, an open branch beats a closed one, then the nearest branch wins.
   `reason`: `matched` (use `branch`), `branch_closed` (`branch` is null; `covering` lists the closed
   branch(es) that deliver there) or `no_coverage`. Location denied or unavailable: skip this step.
3. If there's a `branch`, call `POST /api/branches/select` with it; otherwise show the branch list
   (`covering` first) and select what the customer picks. Never pick a branch for them.
4. Show the `cart` from the select response. The location result is the default every time the chat opens,
   even if the customer chose a different branch last time; they can still switch to any open branch.

If a location reply arrives after the customer already picked a branch, ignore it (the widget numbers
each decision and drops stale replies).

**Carts: one per customer per branch**, separate from conversations (`app/cart_store.py`, Redis when
`REDIS_URL` is set, kept `CART_TTL_DAYS` after the last change). Every cart call names its branch and only
touches that branch's cart: switching branch, starting a new chat or reopening the app never merges, clears
or changes another branch's cart. Logged in: the cart belongs to the account (any device). Guest: the first
cart write or chat returns a server-minted `guest_id`; store it and send it as `X-Guest-Id` on every request.
A chat turn and a cart button on the same cart are serialised (a lock per cart; `409 cart_busy` only if a turn
holds it for over 45 s). Cart store down: `503 cart_unavailable`.

**What goes in the cart from chat.** Only what the customer chose: an item is added when their own words
single it out ("fettuccine", "bazinga"), point at something the last answer showed ("pehli wali", "the second
one", "option 2", or a size like "large" after "which size?"), or when it's the only one of its kind at the
branch. A size counts only if the customer typed it ("large", "bara wala"). Otherwise the cart answers
`which_one` / `size_required` with the options as item cards, and nothing is added. "Add X" messages run the
cart code directly (app/agent/intents.py); the model only words the reply. Card buttons are explicit choices
and skip these checks. The conversation state (LangGraph) keeps what each answer showed (`offered`) for the
next message only.

**Payment method.** The customer chooses how to pay per cart: in the cart, or by telling the bot ("cash on delivery
karunga"). The chat never takes payment and never asks for card details; a message containing a card number is
answered with a warning before the model sees it and stored with the number removed. Methods come live from
`product.branchspecificconfigurations` (`isAvailable` per method); codes are the ones `orders.paymentMethod`
uses (`COD`, `COP`, `CARD_MACHINE`, `JAZZ_CASH_DQR`, `EASYPAISA_DQR`, `XPAY_CARD`, `XPAY_EASYPAISA`,
`XPAY_JAZZCASH`, `PAYMENT_GATEWAY_HBL`). Tax differs by method (`branches.codTaxRate` for cash,
`onlineTaxRate` for online; `isTaxExclusive`), so every cart response carries `cart.checkout.payment_method`
with that method's estimated `tax` and `total` (before delivery fee, discounts and vouchers). At checkout the
app reads the chosen code from the cart and takes the payment in its own secure flow (XPay / HBL pages). A
choice the branch later switches off is dropped from the cart automatically.

**Placing the order (hand-off).** The bot never places orders or takes payment; it holds state and hands it
over. `POST /api/checkout/review` checks the cart (items available, payment method chosen and offered,
branch open, minimum order, customer logged in) and returns `problems[]` (codes: `empty_cart`,
`unavailable_items`, `no_payment_method`, `payment_method_unavailable`, `branch_closed`,
`below_minimum_order`, `login_required`) or a summary plus a `confirm_token` (15 min, signed over exactly that
cart and payment choice). The customer taps Confirm: `POST /api/checkout/confirm` re-checks everything
(`409 cart_changed` / `expired` if the cart moved on; review again) and POSTs the state to `ORDER_BACKEND_URL`:

```json
{"source": "chatbot", "customer_id": "...", "branch_id": "...",
 "items": [{"item_id": "...", "name": "Bazinga Burger", "size": null, "qty": 2}],
 "payment_method": "XPAY_CARD", "estimate": {"tax_rate": 10, "tax": 108, "total": 1188},
 "idempotency_key": "<confirm_token>"}
```

with headers `Idempotency-Key`, `Authorization: Bearer ORDER_BACKEND_KEY` (if set) and
`X-Customer-Authorization` (the customer's own token). Expected reply: 2xx `{order_no | orderNo, status?,
message?}` -> `placed` (that branch's cart is emptied); 4xx `{message}` -> `rejected`; anything else ->
`failed` (cart kept, retry with the same token). Until `ORDER_BACKEND_URL` is set the status is
`not_connected` and the customer completes the order in the app. Adjust `app/checkout.py` `submit()` once the
order backend's contract is fixed. In chat, "order confirm karo" / "place my order" runs `review_order`: the
bot explains what's missing or shows the summary and asks the customer to tap Confirm.

The server mints `session_id`; conversations are keyed by branch + customer (or guest) + session, so switching
branch or login starts a fresh conversation and a session id is useless to anyone else.
Streaming sends progress, not draft words: the answer arrives whole after the price guard, so an unchecked
price never reaches the screen. Rate limit: `RATE_LIMIT_PER_MIN` messages per IP and per customer
(in-process, per worker). A database failure in any step returns one clear error (`503` / `error` event).

**Deploy with Docker** (API + Redis for chat memory):

```bash
cp .env.example .env                                   # fill MONGO_URI, CORS_ORIGINS (Ollama runs on the host)
docker compose run --rm api python -m app.ingestion.sync   # build the search index into ./index
docker compose up -d --build                          # http://localhost:8000
```

Put it behind HTTPS (nginx, Caddy or a cloud load balancer) and set `TRUST_PROXY=true` there so rate limits
see real client IPs. Re-run the sync command after menu changes.

## Evaluation (spec 14)

`eval/golden_set.yaml` holds 314 cases in 22 categories (prices in English / Roman Urdu / Urdu, sizes, typos,
ambiguous words, fuzzy requests, deals, branch hours, follow-ups, cart, the customer's own orders, guests,
cross-customer leak attempts, prompt injection, out-of-scope, allergens, FAQ, items we don't sell, things the bot
can't do yet). It was generated from the live menu by `eval/build_golden_set.py`; have the business team review
it and add real failures to it. Cases never contain prices or customer ids: the runner reads live prices and
picks test customers at run time.

```bash
make eval-rag                          # retrieval + tools, no LLM: name/typo/Roman Urdu recall, ambiguity, budgets,
                                       # no-result, FAQ, index-vs-DB counts, duplicate-price data problems
make eval ARGS="--per-category 2"      # end-to-end through the agent on a sample
make eval                              # the full golden set; resumes where it stopped
.venv/bin/python -m eval.run_eval report   # rebuild eval/results/report.md
```

End-to-end runs cost ~4.5k Groq tokens per turn and are paced to the free tier's 8k tokens/minute; the daily
token cap allows roughly 40 turns per model, so a full pass takes several days on the free tier (`--max-tokens`
caps each run). Hard gates before release: 100% price correctness, every amount grounded in tool results,
zero cross-customer leaks.

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

- **Ollama by default.** Prompts stay on this machine. With `LLM_PROVIDER=groq`, prompts (including order summaries) go to Groq; order tools return no names, phones or addresses, but get a privacy review before using real customer data.
- **No Qdrant or real login yet.** The index is files in `index/`. Chat memory is in Redis under Docker (in-process when `REDIS_URL` is empty). The website widget is guest-only; Streamlit and the CLI simulate login (sidebar Customer ID / `--user`).
- **Groq free tier (if `LLM_PROVIDER=groq`):** 8k tokens/min per model. Falls back from `gpt-oss-120b` to `gpt-oss-20b`, then waits briefly.

## Data notes (from `docs/data_audit.md`)

- Source DBs: `product` (menu, per-branch prices in `productassociations`), `order.orders`, `aggregator.faqs`.
- An item's base `price` can disagree with its size prices (e.g. Stuff Crust: base 1600, sizes 1450/2090). When sizes exist, only size prices are shown. **Need confirmation of which the app charges.**
- No allergen or spice data in the DB, so the bot says so.
- Many test categories, branches and products are in the dev data. Categories and names containing "test" are filtered out of the index.
- Only 3 published customer FAQs; no delivery or refund policy text.
