# Phase 7 — Cost and token accounting

## What was built
- `obsdash/pricing.py` — `PricingTable` (loaded from your JSON file) and `compute()` using exact `Decimal` math. Prices are per million tokens.
- `obsdash/ai_telemetry.py` — every model call / embedding now gets a cost status. Span attrs: `obsdash.ai.cost_status` (known / unknown_model / no_usage), and when known `obsdash.ai.cost_amount` + `obsdash.ai.cost_currency`. Repeated `set_usage()` calls now ADD up (streaming).
- Metrics: `obsdash_ai_cost_total{provider,target,currency}` (known prices only) and `obsdash_ai_unpriced_operations_total{provider,target,reason}`.
- `obsdash/api_costs.py` — `GET /api/costs/summary?window_minutes=` : per provider/model calls, priced vs unpriced calls, tokens, cost by currency, totals.
- `pricing.example.json` — fake provider, made-up numbers, for demo/tests only.
- Config: `PRICING_TABLE_PATH` (empty = no prices known).

## Rules (why it is built this way)
1. **No built-in prices.** I can't verify today's provider prices, so none are shipped. You fill the file from the providers' pricing pages.
2. **Unknown model -> cost unknown, never 0.** Counted in `unpriced`, so spend is never silently under-reported.
3. **No token counts -> unknown, never estimated.**
4. **A genuinely free model (price 0) is known, cost 0.**
5. A failed call that reported no tokens is neither costed nor counted as unpriced.
6. A bad pricing file stops the app at startup with a clear error.

## Commands
```
python -m pytest -q
set PRICING_TABLE_PATH=pricing.example.json       (PowerShell: $env:PRICING_TABLE_PATH="pricing.example.json")
python -m uvicorn obsdash.main:app --port 8100
curl http://localhost:8100/api/costs/summary
curl http://localhost:8100/metrics
```
The running app has no endpoint that makes AI calls yet (integrations are Phase 11), so the summary is empty live; the tests and the in-process check below use fakes.

## Results
83 passed (54 + 29 new). In-process check with the example table: 1 call with usage (1,000,000 in / 500,000 out) -> cost 2.0 USD; 1 call with no usage -> unpriced (no_usage); summary and /metrics agree.

## Honest limits
- Summary is built from spans still in the bounded trace store, so it covers the retained window, not all-time spend. Prometheus counters hold running totals while the process lives (reset on restart).
- Prices are only as right as your file; `effective_date` is stored but historical re-pricing is not implemented.
- Currencies are never converted; mixed currencies are reported separately.
- Cached-token / batch / tiered discounts are not modelled: only input and output rates.
- Float conversion at the edge (spans/metrics) is rounded to 8 places.
