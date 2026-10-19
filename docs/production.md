# Serving the assistant in production

How the prompt is built, how the bot is kept from making things up, and what has to be in place before it
serves hundreds of thousands of requests.

## 1. Prompt: built per message from parts

| Part | File | Sent |
|---|---|---|
| Core: identity, scope, truth rules, language, safety | `app/agent/prompts/core.md` | always, first (never changes) |
| Rule packs: menu, cart, payment, checkout, branch, orders, FAQ | `app/agent/prompts/packs/*.md` | only the packs for this message's topics, fixed order |
| Context: branch, login, time, language, items just shown | `app/agent/prompts/packs/context.md` | always, last |
| Tools | `app/tools.py` | only the tools of those topics; none when the code already ran the lookup |
| History | conversation state | last `HISTORY_TURNS` exchanges, at most `HISTORY_MAX_CHARS` characters |

Topics come from `app/agent/intents.py` (`topics()`); an unrecognised message gets every pack and tool, never
fewer. Stable text comes first so providers (and Ollama) can reuse the start of the prompt. Prompt files are
read once per process; branch name/city is cached 60 s.

Measured on Groq (prompt + tools, before history): 2,978 tokens for every message before; now 1,179 (menu
question), 1,359 (cart), 1,550 (payment), 1,938 (orders); unrecognised messages stay ~3,000.

## 2. Keeping it from making things up (layers)

1. **Prompt contract** (`core.md` `<truth>`): facts only from this turn's tool results or the context block.
2. **Code runs the obvious lookups first** (`intents.py`): cart, menu/category/price/budget searches, deals,
   best sellers, branch, payment, checkout. The model words the result.
3. **Tools check choices against the customer's own words** (`app/cart.py`, `app/payments.py`,
   `app/checkout.py`): nothing goes in the cart, no size or payment method is set, unless the customer chose it.
4. **Every reply is checked before it is sent** (`app/agent/guards.py`): prices must appear in tool results;
   menu items mentioned must have been looked up; items presented in lists or bold must exist on the menu or in
   tool results (catches invented dishes like "Spaghetti"); branch/order claims need their tool; reply language.
5. **One retry, then an honest fallback** in the customer's language, with no item cards: cards only come with a real answer that gives them context.
6. **Hard limits in code**: card numbers never reach the model; identity only from the JWT; order hand-off only
   on an explicit tap with a token tied to the exact cart.

Watch in production: the share of replies the checks reject (`guard_problems` in traces) is the hallucination
rate. A rise after a prompt or model change means something regressed.

## 3. Scale checklist

Done in code:
- Stateless API; chat memory and carts in Redis (`REDIS_URL`), so any worker can serve any request.
- Per-request token budget: topic packs, tool subsets, history capped by size, `num_predict`/`max_tokens` 1024.
- Time budget per turn (`LLM_TIME_BUDGET_S`) with fallbacks; Groq fallback model on rate limits.
- Live prices/availability from Mongo on every answer; short caches only for slow-changing data (branch list
  5 min, payment config 60 s, branch name 60 s).
- Idempotent order hand-off (`Idempotency-Key`), per-cart locks, PII redaction in traces.

Before launch:
- **LLM capacity.** Each chat turn is 1-2 model calls of ~1.5-4k tokens. 300k turns/day is roughly 1-2 billion
  input tokens a day: a paid provider tier (or self-hosted vLLM on GPUs with batching) is required; the free
  tier allows ~8k tokens a minute. Add a second provider as failover.
- **Rate limiting in Redis.** `RateLimiter` in `app/api.py` is per process; with several workers/servers it must
  move to Redis to be a real per-customer limit.
- **Workers and concurrency.** Model calls block a worker thread for seconds; size the thread pool / number of
  workers for the expected concurrent chats, or move the model calls to async. Load-test before launch.
- **Mongo.** Indexes on `productassociations (branchId, productId)` and `orders (customerId, createdAt)`;
  a read-only user (already) on a replica; watch query times (`QUERY_TIMEOUT_MS`).
- **Observability.** `logs/traces.jsonl` is a local file: ship structured traces to a log store and track
  latency, model errors, rate limits, fallback rate and guard rejections, with alerts.
- **Menu changes.** Rebuild the search index (`make index`) whenever the menu changes, e.g. on a schedule.
- **Evaluation.** Run `make eval-rag` and a sample of `make eval` on every prompt, synonym or model change.
