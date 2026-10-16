# Phase 12 — Testing: unit, integration, end-to-end

## Layout
- `tests/unit/` (193) — one module per component (config, middleware, tracing, metrics, store, pricing, alerts, security...).
- `tests/integration/` (29) — OTLP receiver at wire level, real OTLP exporter -> live server, production-rag's real tracing code in a subprocess, eval-harness observer driven by its real `run_evaluation`.
- `tests/e2e/` (7) — synthetic service scenarios through the whole pipeline. No paid AI calls (fakes). Only the alert clock is simulated.

## E2E scenarios and what each cross-checks
| Scenario | Generated | Verified across surfaces |
|---|---|---|
| success | 20 requests, each: retrieval, rerank, embedding, model call | Prometheus counters, token totals, exact USD cost; /api/overview; /api/costs; request log lines <-> stored trace ids; trace tree (5 spans, parent/child); one request count agreed by 4 sources |
| slow | 10 fast + 10 `sleep(0.25)` | per-route p95 >= 250 ms, latency histogram count, slow alert FIRING, error alert stays OK |
| failure | 15 ok + 10 failing model calls | 5xx counters, AI error counters, error rate 0.4, no fabricated cost for failed calls, one error origin per failed request, 10 `request_error` logs each matching a stored ERROR trace, alert lifecycle None->PENDING->FIRING->OK + gauge 1 -> 0 |
| unknown model | 6 calls to an unpriced model | tokens counted, cost unknown (not 0), unpriced counter, alert |
| restart | 5 requests, reopen the SQLite file | same totals after restart |
| empty system | nothing | error rate / p95 are `null` (unknown, not 0); rules NO_DATA |
| dashboard page | GET /dashboard | strict CSP |

## Running
```
python -m pytest -q                       # everything (siblings are found next to this repo, else those tests skip)
python -m pytest tests/e2e -q
pip install coverage && python -m coverage run --source=obsdash -m pytest -q && python -m coverage report -m
# optional real-browser check (needs playwright + chromium):
python scripts/seed_demo_data.py demo.db 200
set SQLITE_PATH=demo.db & python -m uvicorn obsdash.main:app --port 8100
python scripts/browser_check.py http://127.0.0.1:8100
```

## How we know the tests can fail (mutation spot-check, run by hand in the sandbox)
Eight deliberate bugs were injected one at a time; the suite caught seven:
unknown model priced as 0; alert ignoring `for_seconds`; percentile off-by-one; redaction disabled; raw URL path
as metric label; unauthenticated OTLP endpoint; firing alert resolving on no data.
The eighth (removing the early size check inside `_read_limited`) was NOT caught, and legitimately so: it is an
equivalent mutant — the streaming loop already enforces the same limit before that function runs, and the
decompressed-size guard remains. It is defense in depth, not a gap.

## Defects found this phase
Phase 11 found one (root span naming on timestamp ties). Phase 12 found none in product code: every failing run during
development was a mistake in the new tests (wrong query parameter, wrong Prometheus label name, wrong arithmetic,
log-capture ordering), corrected in the tests.

## Honest limits
- Coverage 99% of lines (1421 statements, 10 uncovered: small defensive branches). Line coverage is not correctness.
- Latency tests rely on `sleep` lower bounds (never upper bounds), so they are not timing-flaky.
- The browser check is outside pytest, needs Playwright, and confirms: tabs render, 0 console/CSP errors, total and error counts
  shown match the API (substring match, so deliberately coarse), a trace row opens. It is not pixel or accessibility testing.
- Load/soak at scale and multi-process write contention on SQLite are not tested.
