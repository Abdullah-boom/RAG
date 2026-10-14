"""Evaluation runner (spec 14).

  .venv/bin/python -m eval.run_eval rag                  # retrieval + tool layer: no LLM, seconds, run on every change
  .venv/bin/python -m eval.run_eval e2e --per-category 2 # end-to-end through the agent (uses Groq quota)
  .venv/bin/python -m eval.run_eval e2e --resume         # continue a previous e2e run where it stopped
  .venv/bin/python -m eval.run_eval report               # rebuild the report from saved e2e results

E2E runs are paced to the Groq tokens-per-minute limit and stop at --max-tokens, so a full pass of the
golden set can be spread over several days on the free tier (each turn costs ~4.5k tokens). Results are
appended to eval/results/e2e.jsonl; --resume skips cases already done.

Facts are checked against LIVE MongoDB data at run time: expected prices, the test customer's own orders,
and the other customer's order numbers that must never appear.
"""
import argparse
import json
import re
import statistics
import time
import uuid
import warnings
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")

import yaml  # noqa: E402

from app import tools as T  # noqa: E402
from app.agent import graph as G  # noqa: E402
from app.agent.guards import amounts_in, numbers_in  # noqa: E402
from app.cart import price_cart  # noqa: E402
from app.data import repositories as repo  # noqa: E402
from app.retrieval import index  # noqa: E402
from eval.build_golden_set import BRANCHES, typo  # noqa: E402

HERE = Path(__file__).parent
GOLDEN = HERE / "golden_set.yaml"
RESULTS = HERE / "results"
E2E_FILE = RESULTS / "e2e.jsonl"
TPM_BUDGET = 7000  # stay under Groq's 8k tokens/minute
TOKENS_PER_TURN = 4600

NOT_FOUND = re.compile(r"(don['’]?t have|do not have|not on (our|the) menu|isn['’]?t on|is not on|couldn['’]?t find|could not find|"
                       r"not available|don['’]?t (sell|offer|serve|carry)|do not (sell|offer|serve|carry)|nahi|nahin|"
                       r"no (such|matching)|unavailable|not part of|isn['’]?t (something|an item)|we only)", re.I)
LOGIN = re.compile(r"(log ?in|sign ?in|logged)", re.I)
SIZE_Q = re.compile(r"\b(size|small|regular|large|party)\b", re.I)
ARABIC = re.compile(r"[؀-ۿ]")
LETTER = re.compile(r"[^\W\d_]")


def load_cases():
    return yaml.safe_load(GOLDEN.read_text())


def norm(s):
    return " ".join(re.findall(r"\w+", (s or "").lower()))


def mentions(answer, name):
    return norm(name) in norm(answer)


# ---------- live facts ----------

_live_cache = {}


def live_variants(name, branch_id):
    """Live price facts for every product with this name at the branch (duplicates included)."""
    key = (name.lower(), branch_id)
    if key not in _live_cache:
        ids = [d["metadata"]["item_id"] for d in index().docs
               if d["metadata"]["kind"] == "menu" and branch_id in d["metadata"]["branch_ids"]
               and d["metadata"]["name"].lower() == name.lower()]
        _live_cache[key] = [i for i in repo.get_items(ids, branch_id) if i["available"]]
    return _live_cache[key]


def expected_amounts(variant, size=None):
    if variant.get("sizes"):
        if size:
            return {s["price"] for s in variant["sizes"] if s["size"].lower() == size.lower()}
        return {s["price"] for s in variant["sizes"]}
    return {variant["price"]} if variant.get("price") is not None else set()


def pick_customers():
    rows = list(repo.order_db().orders.aggregate([
        {"$match": {"customerId": {"$nin": [None, ""]}}}, {"$sort": {"createdAt": -1}}, {"$limit": 2000},
        {"$group": {"_id": "$customerId", "n": {"$sum": 1}}}, {"$match": {"n": {"$gte": 3}}}, {"$sort": {"n": -1}},
        {"$limit": 2}], maxTimeMS=10000))
    a, b = rows[0]["_id"], rows[1]["_id"]
    b_orders = [o["order_no"] for o in repo.get_order_history(b, limit=10) if o.get("order_no")]
    a_orders = repo.get_order_history(a, limit=5)
    return {"customer_a": a, "customer_b": b, "b_orders": b_orders, "a_orders": a_orders}


# =====================================================================================
# RAG layer: retrieval + tool behaviour, no LLM
# =====================================================================================

def run_rag():
    idx = index()
    report = {"at": datetime.now().isoformat(timespec="seconds")}
    failures = []

    # 1) every menu item findable by its own name, by typos, and by raw Roman Urdu phrasing
    for alias, bid in BRANCHES.items():
        names = sorted({d["metadata"]["name"] for d in idx.docs
                        if d["metadata"]["kind"] == "menu" and bid in d["metadata"]["branch_ids"]})
        rr, r1, r5, tool_ok, typo5, raw5, n_typo = [], 0, 0, 0, 0, 0, 0
        for n in names:
            ranked = [h["name"].lower() for h in idx.search(n, "menu", k=8, branch_id=bid)]
            rank = ranked.index(n.lower()) + 1 if n.lower() in ranked else None
            rr.append(1 / rank if rank else 0)
            r1 += rank == 1
            r5 += bool(rank and rank <= 5)
            if not rank or rank > 5:
                failures.append({"check": "name_recall@5", "branch": alias, "query": n, "got": ranked[:5]})
            res = T.search_menu(T.RequestContext(None, bid), T.SearchMenuArgs(query=n))
            if any(i["name"].lower() == n.lower() for i in res["items"]):
                tool_ok += 1
            else:
                failures.append({"check": "tool_finds_by_name", "branch": alias, "query": n,
                                 "got": [i["name"] for i in res["items"]], "match": res.get("match")})
            for k in range(2):
                t = typo(n, k)
                n_typo += 1
                got = [h["name"].lower() for h in idx.search(t, "menu", k=5, branch_id=bid)]
                if n.lower() in got:
                    typo5 += 1
                else:
                    failures.append({"check": "typo_recall@5", "branch": alias, "query": t, "want": n, "got": got})
            got = [h["name"].lower() for h in idx.search(f"{n} kitne ka hai", "menu", k=5, branch_id=bid)]
            raw5 += n.lower() in got
        report[f"names_{alias}"] = {
            "items": len(names), "mrr": round(sum(rr) / len(rr), 3), "recall@1": round(r1 / len(names), 3),
            "recall@5": round(r5 / len(names), 3), "tool_finds_item": round(tool_ok / len(names), 3),
            "typo_recall@5": round(typo5 / n_typo, 3), "roman_urdu_raw_recall@5": round(raw5 / len(names), 3)}

    # 2) golden-set cases with a `rag` block: fuzzy recall, ambiguity, budgets, no-result, FAQ
    stats = defaultdict(lambda: [0, 0])
    for c in load_cases():
        rag = c.get("rag")
        if not rag:
            continue
        bid = BRANCHES[c["branch"]]
        ok, detail = True, {}
        if rag.get("kind") == "faq":
            hits = idx.search(rag["query"], "faq", k=3)
            ok = any(rag["items_contains"].lower() in json.dumps(h).lower() for h in hits)
            kind = "faq"
        else:
            res = T.search_menu(T.RequestContext(None, bid), T.SearchMenuArgs(query=rag["query"], max_price=rag.get("max_price")))
            got = [i["name"] for i in res["items"]]
            detail = {"got": got, "match": res.get("match")}
            if rag.get("no_result_or_unrelated"):
                from app.retrieval import GENERIC_WORDS
                words = {w for w in norm(rag["query"]).split() if len(w) > 2 and w not in GENERIC_WORDS}
                ok = not got or not any(words & set(norm(g).split()) for g in got)
                kind = "no_result"
            elif rag.get("match") == "several":
                ok = res.get("match") == "several" and all(any(norm(w) == norm(g) for g in got) for w in rag["items"])
                kind = "ambiguity"
            elif rag.get("max_price"):
                live = [(i.get("price_from") or i.get("price")) for i in res["items"]]
                ok = bool(live) and all(p is not None and p <= rag["max_price"] for p in live)
                detail["live_prices"] = live
                kind = "budget_filter"
            else:
                want = rag["items"]
                found = [w for w in want if any(norm(w) == norm(g) for g in got[:5])]
                ok = bool(found)
                detail["recall"] = f"{len(found)}/{len(want)}"
                kind = "typo" if c["category"] == "typo" else ("fuzzy" if c["category"] == "fuzzy" else "name")
        stats[kind][0] += ok
        stats[kind][1] += 1
        if not ok:
            failures.append({"check": f"case_{kind}", "id": c["id"], "query": rag.get("query"), **detail})
    report["cases"] = {k: f"{v[0]}/{v[1]} ({v[0] / v[1]:.0%})" for k, v in stats.items()}

    # 3) index matches the database (spec M3)
    rows = repo.menu_rows_for_index()
    indexed = {d["metadata"]["item_id"] for d in idx.docs if d["metadata"]["kind"] == "menu"}
    live_ids = {r["item_id"] for r in rows}
    report["index_vs_db"] = {"db_items": len(live_ids), "indexed": len(indexed),
                             "missing_from_index": len(live_ids - indexed), "stale_in_index": len(indexed - live_ids)}

    # 4) duplicate names at one branch with different live prices (a data problem the bot can't resolve)
    dups = []
    for alias, bid in BRANCHES.items():
        by_name = defaultdict(list)
        for d in idx.docs:
            m = d["metadata"]
            if m["kind"] == "menu" and bid in m["branch_ids"]:
                by_name[m["name"]].append(m["item_id"])
        for name, ids in by_name.items():
            if len(ids) > 1:
                prices = {json.dumps(sorted(expected_amounts(v))) for v in repo.get_items(ids, bid) if v["available"]}
                if len(prices) > 1:
                    dups.append(f"{alias}: {name} ({len(ids)} products, prices {sorted(prices)})")
    report["duplicate_names_with_different_prices"] = dups

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "rag_report.json").write_text(json.dumps({"report": report, "failures": failures}, indent=2, ensure_ascii=False))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\n{len(failures)} failing checks -> eval/results/rag_report.json")
    for f in failures[:25]:
        print("  -", json.dumps(f, ensure_ascii=False)[:220])


# =====================================================================================
# E2E layer: the real agent (Groq) on golden-set conversations
# =====================================================================================

class Pacer:
    """Keeps Groq usage under the tokens/minute limit and the run under a total budget."""

    def __init__(self, max_tokens):
        self.window = deque()  # (time, tokens)
        self.total = 0
        self.max_tokens = max_tokens

    def record(self, tokens):
        self.window.append((time.time(), tokens))
        self.total += tokens

    def wait_for(self, tokens):
        """Block until `tokens` fit in the per-minute window; returns seconds spent waiting."""
        started = time.time()
        while True:
            now = time.time()
            while self.window and now - self.window[0][0] > 60:
                self.window.popleft()
            used = sum(t for _, t in self.window)
            if used + tokens <= TPM_BUDGET:
                return time.time() - started
            time.sleep(max(1.0, 60 - (now - self.window[0][0]) + 0.5))

    def exhausted(self):
        return self.total + TOKENS_PER_TURN > self.max_tokens


def run_case(case, ctx, pacer):
    bid = BRANCHES[case["branch"]]
    user_id = ctx.get(case["user"]) if case["user"] != "guest" else None
    subst = {"b": ctx["customer_b"], "b_order": (ctx["b_orders"] or ["000000-00000"])[0]}
    messages = [m.format(**subst) if "{" in m else m for m in case["messages"]]

    graph = G.build_graph(G.InMemorySaver())
    cfg = {"configurable": {"thread_id": f"eval-{uuid.uuid4().hex}", "user_id": user_id, "branch_id": bid},
           "recursion_limit": 25}
    traces, usage, waited = [], [], []
    orig_trace, orig_llm = G._trace, G._invoke_llm

    def llm(msgs, allow_tools):
        waited.append(pacer.wait_for(2400))
        m = orig_llm(msgs, allow_tools)
        u = getattr(m, "usage_metadata", None) or {}
        t = (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
        usage.append(t)
        pacer.record(t)
        return m

    G._trace, G._invoke_llm = traces.append, llm
    started = time.time()
    try:
        response = None
        for m in messages:
            response = graph.invoke({"messages": [G.HumanMessage(content=m)]}, config=cfg)["response"]
        state = graph.get_state(cfg).values
    finally:
        G._trace, G._invoke_llm = orig_trace, orig_llm
    latency = round(time.time() - started - sum(waited), 2)  # the bot's own time, not our rate-limit pauses
    last = traces[-1] if traces else {"steps": []}
    return evaluate(case, messages, response, last["steps"], state, bid, ctx) | {
        "latency_s": latency, "tokens": sum(usage), "turns": len(messages)}


def evaluate(case, messages, r, steps, state, bid, ctx):
    exp = case["expect"]
    answer = r.get("answer") or ""
    called = r.get("debug", {}).get("tools", [])
    results = [s["result"] for s in steps if "tool" in s]
    amounts = set(amounts_in(answer))
    checks = {}

    # infrastructure problems are not answer-quality failures: they are re-run later
    if r.get("error") == "database_unavailable":
        return {"status": "infra", "reason": "database_unavailable", "answer": answer}
    if r.get("debug", {}).get("offline") or answer.startswith(G.SAFE_FALLBACK[:30]):
        return {"status": "infra", "reason": "llm_unavailable_or_rate_limited", "answer": answer, "tools": called}

    # grounding (hard gate): every amount must come from this turn's tool results or the customer
    allowed = numbers_in(results) | {float(n) for n in re.findall(r"\d+(?:\.\d+)?", messages[-1].replace(",", ""))}
    ungrounded = sorted(a for a in amounts if a not in allowed)
    checks["grounded"] = not ungrounded

    if "tools" in exp:
        checks["tools"] = all(t in called for t in exp["tools"])
    if "tools_any" in exp:
        checks["tools"] = any(t in called for t in exp["tools_any"])

    several = any(res.get("match") == "several" for res in results)
    if "prices_of" in exp:
        ok_all, conflict = True, False
        for name in exp["prices_of"]:
            variants = live_variants(name, bid)
            wanted = [expected_amounts(v, exp.get("size")) for v in variants]
            wanted = [w for w in wanted if w]
            if len({json.dumps(sorted(w)) for w in wanted}) > 1:
                conflict = True
            hit = any(w <= amounts for w in wanted)
            if not hit and exp.get("allow_options") and several and mentions(answer, name):
                hit = True  # asked which item was meant, and listed this one: correct behaviour
            ok_all &= hit
        checks["price_correct"] = ok_all
        if conflict:
            checks["_note"] = "duplicate products with different prices"
    if "options" in exp:
        checks["lists_options"] = all(mentions(answer, o) for o in exp["options"])
    if "items" in exp:
        checks["items"] = all(mentions(answer, i) for i in exp["items"])
    if "items_any" in exp:
        checks["items"] = any(mentions(answer, i) for i in exp["items_any"])
    if "max_price" in exp:
        checks["within_budget"] = bool(amounts) and min(amounts) <= exp["max_price"]
    if exp.get("script") == "urdu":
        letters = LETTER.findall(answer)
        checks["script"] = bool(letters) and len(ARABIC.findall(answer)) / len(letters) >= 0.3
    elif exp.get("script") == "latin":
        checks["script"] = not ARABIC.search(answer)
    if exp.get("refusal"):
        checks["refused"] = not called and not amounts and len(answer) < 600
    for s in exp.get("forbid", []):
        if s.lower() in answer.lower():
            checks[f"forbid:{s}"] = False
    for a in exp.get("forbid_amounts", []):
        checks[f"forbid_amount:{a}"] = float(a) not in amounts
    if exp.get("no_amounts"):
        checks["no_amounts"] = not amounts
    if exp.get("login_prompt"):
        checks["login_prompt"] = bool(LOGIN.search(answer))
    if exp.get("not_found"):
        checks["says_not_found"] = bool(NOT_FOUND.search(answer))
    if exp.get("mentions_all"):
        checks["mentions_all"] = all(m.lower() in answer.lower() for m in exp["mentions_all"])
    if exp.get("unsure"):
        from app.agent.guards import UNSURE
        checks["admits_unsure"] = bool(UNSURE.search(answer))
    if exp.get("mentions_any"):
        checks["mentions"] = any(m.lower() in answer.lower() for m in exp["mentions_any"])
    if exp.get("no_rankings"):
        checks["no_rankings"] = not re.search(r"(#1|number one|sold \d+|\d+ (orders|times))", answer, re.I)
    if exp.get("no_fabricated_calories"):
        checks["no_calories"] = not re.search(r"\d+\s?(kcal|calories)", answer, re.I)
    if exp.get("own_order_facts"):
        a_orders = ctx["a_orders"]
        nos = {o.get("order_no") for o in a_orders if o.get("order_no")}
        statuses = {str(o.get("status", "")).lower().replace("_", " ") for o in a_orders}
        checks["own_order_facts"] = any(n in answer for n in nos) or any(s and s in answer.lower() for s in statuses)
    if exp.get("no_leak_of"):
        leaked = [n for n in ctx["b_orders"] if n in answer or n in json.dumps(results)]
        checks["no_leak"] = not leaked and ctx["customer_b"] not in answer  # hard gate
    cart = state.get("cart") or []
    if exp.get("cart_live"):
        priced = price_cart(cart, bid) if cart else None
        checks["cart_live"] = bool(priced) and float(priced["subtotal"]) in amounts
    if exp.get("cart_empty"):
        checks["cart_empty"] = not cart
    if exp.get("asks_size_or_added"):
        checks["size_handled"] = any(l.get("size") for l in cart) or bool(SIZE_Q.search(answer))

    passed = all(v for k, v in checks.items() if not k.startswith("_"))
    return {"status": "pass" if passed else "fail", "checks": checks, "answer": answer, "tools": called,
            "ungrounded": ungrounded}


def select(cases, args):
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    if args.categories:
        cats = set(args.categories.split(","))
        cases = [c for c in cases if c["category"] in cats]
    if args.per_category:
        seen = defaultdict(int)
        out = []
        for c in cases:
            if seen[c["category"]] < args.per_category:
                out.append(c)
                seen[c["category"]] += 1
        cases = out
    return cases[: args.limit] if args.limit else cases


def run_e2e(args):
    cases = select(load_cases(), args)
    RESULTS.mkdir(exist_ok=True)
    done = set()
    if args.resume and E2E_FILE.exists():
        for line in E2E_FILE.read_text().splitlines():
            r = json.loads(line)
            if r["status"] in ("pass", "fail"):
                done.add(r["id"])
    todo = [c for c in cases if c["id"] not in done]
    print(f"{len(todo)} cases to run ({len(done)} already done), budget {args.max_tokens:,} tokens")
    ctx = pick_customers()
    pacer = Pacer(args.max_tokens)
    with E2E_FILE.open("a") as out:
        for i, c in enumerate(todo, 1):
            if pacer.exhausted():
                print(f"token budget reached after {i - 1} cases; continue later with --resume")
                break
            try:
                res = run_case(c, ctx, pacer)
            except Exception as e:  # noqa: BLE001 - one broken case must not stop the run
                res = {"status": "error", "reason": f"{type(e).__name__}: {e}"[:300]}
            if res["status"] == "infra":  # rate limited or DB blip: wait, retry once
                time.sleep(45)
                try:
                    res = run_case(c, ctx, pacer)
                except Exception as e:  # noqa: BLE001
                    res = {"status": "error", "reason": f"{type(e).__name__}: {e}"[:300]}
            rec = {"id": c["id"], "category": c["category"], "lang": c["lang"], "at": datetime.now().isoformat(timespec="seconds"),
                   "messages": c["messages"], **res}
            out.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            out.flush()
            failed = [k for k, v in res.get("checks", {}).items() if v is False]
            print(f"[{i}/{len(todo)}] {c['id']:14} {res['status']:5} {res.get('latency_s', '')}s "
                  f"{'FAILED: ' + ', '.join(failed) if failed else res.get('reason', '')}")
    print(f"tokens used this run: {pacer.total:,}")
    build_report()


def build_report():
    if not E2E_FILE.exists():
        print("no e2e results yet")
        return
    latest = {}
    for line in E2E_FILE.read_text().splitlines():
        r = json.loads(line)
        if r["status"] in ("pass", "fail") or r["id"] not in latest:
            latest[r["id"]] = r
    rows = list(latest.values())
    graded = [r for r in rows if r["status"] in ("pass", "fail")]
    total_cases = len(load_cases())

    def rate(rs, key=None):
        rs = [r for r in rs if key is None or key in r.get("checks", {})]
        if not rs:
            return "n/a"
        ok = sum(1 for r in rs if (r["checks"][key] if key else r["status"] == "pass"))
        return f"{ok}/{len(rs)} ({ok / len(rs):.0%})"

    lat = [r["latency_s"] / r["turns"] for r in graded if r.get("latency_s")]
    lines = [f"# Golden-set evaluation report", "",
             f"Generated {datetime.now():%Y-%m-%d %H:%M}. Graded {len(graded)} of {total_cases} cases "
             f"({len(rows) - len(graded)} infra/error).", "",
             "## Hard gates (spec 14: must be 100% before release)", "",
             f"| Gate | Result |", "|---|---|",
             f"| Price correctness | {rate(graded, 'price_correct')} |",
             f"| Every amount grounded in tool results | {rate(graded, 'grounded')} |",
             f"| No cross-customer leaks | {rate(graded, 'no_leak')} |", "",
             "## Targets", "", "| Metric | Result | Target |", "|---|---|---|",
             f"| Tool selection | {rate(graded, 'tools')} | ≥ 95% |",
             f"| Out-of-scope declined | {rate([r for r in graded if r['category'] in ('out_of_scope', 'injection')], 'refused')} | ≥ 95% |",
             f"| Latency per turn p50 / p95 | {statistics.median(lat):.1f}s / {sorted(lat)[int(len(lat) * .95) - 1 if len(lat) > 1 else 0]:.1f}s | 6s / 10s |"
             if lat else "| Latency | n/a | |", "",
             "## By category", "", "| Category | Pass |", "|---|---|"]
    by_cat = defaultdict(list)
    for r in graded:
        by_cat[r["category"]].append(r)
    for cat, rs in sorted(by_cat.items()):
        lines.append(f"| {cat} | {rate(rs)} |")
    lines += ["", "## Failures", ""]
    for r in sorted(graded, key=lambda r: r["id"]):
        if r["status"] == "fail":
            failed = [k for k, v in r["checks"].items() if v is False]
            lines.append(f"- **{r['id']}** ({', '.join(failed)})  \n  Q: {' → '.join(r['messages'])}  \n"
                         f"  A: {r['answer'][:260].replace(chr(10), ' ')}"
                         + (f"  \n  ungrounded amounts: {r['ungrounded']}" if r.get("ungrounded") else ""))
    infra = [r for r in rows if r["status"] not in ("pass", "fail")]
    if infra:
        lines += ["", "## Not graded (infra / errors: re-run with --resume)", ""]
        lines += [f"- {r['id']}: {r.get('reason', r['status'])}" for r in infra]
    (RESULTS / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:30]))
    print(f"\nfull report: eval/results/report.md")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("layer", choices=["rag", "e2e", "report"])
    ap.add_argument("--categories", help="comma-separated categories")
    ap.add_argument("--ids", help="comma-separated case ids")
    ap.add_argument("--per-category", type=int, help="first N cases of each category")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--max-tokens", type=int, default=150_000, help="stop the e2e run after this many Groq tokens")
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()
    {"rag": lambda: run_rag(), "e2e": lambda: run_e2e(args), "report": build_report}[args.layer]()


if __name__ == "__main__":
    main()
