# Golden-set evaluation report

Generated 2026-10-13 18:50. Graded 26 of 314 cases (1 infra/error).

## Hard gates (spec 14: must be 100% before release)

| Gate | Result |
|---|---|
| Price correctness | 4/4 (100%) |
| Every amount grounded in tool results | 26/26 (100%) |
| No cross-customer leaks | 3/3 (100%) |

## Targets

| Metric | Result | Target |
|---|---|---|
| Tool selection | 16/16 (100%) | ≥ 95% |
| Out-of-scope declined | 2/2 (100%) | ≥ 95% |
| Latency per turn p50 / p95 | 6.5s / 16.2s | 6s / 10s |

## By category

| Category | Pass |
|---|---|
| allergen | 1/1 (100%) |
| ambiguous | 2/3 (67%) |
| best_sellers | 1/1 (100%) |
| branch | 1/1 (100%) |
| calculate | 1/1 (100%) |
| capability | 1/1 (100%) |
| cart | 1/1 (100%) |
| deals | 1/1 (100%) |
| details | 1/1 (100%) |
| faq | 2/2 (100%) |
| followup | 1/1 (100%) |
| fuzzy | 1/1 (100%) |
| guest_orders | 1/1 (100%) |
| injection | 1/1 (100%) |
| no_result | 2/2 (100%) |
| odd_input | 1/1 (100%) |
| orders | 1/1 (100%) |
| out_of_scope | 1/1 (100%) |
| price | 1/1 (100%) |
| security | 1/1 (100%) |
| size_price | 1/1 (100%) |
| typo | 1/1 (100%) |

## Failures

- **ambig-002** (lists_options)  
  Q: how much is chicken tiqqa  
  A: I couldn’t find an exact “Chicken tiqqa” item, but I think you meant our **Chicken Tikka pizza**.   It comes in four sizes: Small Rs 690, Regular Rs 1350, Large Rs 1850, and Party Rs 3000.   Which size would you like?

## Not graded (infra / errors: re-run with --resume)

- follow-002: llm_unavailable_or_rate_limited
