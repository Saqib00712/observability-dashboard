# obsdash — final acceptance review and engineering report

**Read this first.** The original wording of the acceptance criteria (Section 14) and the 18-item report format (Section 15)
was lost when the working session was compacted, so this document is organised by the components the project was specified
to deliver (as recorded in my notes), not by the original item numbers. If you paste the original lists I will re-map this
review one-to-one. Status words: **PASS** = demonstrated by a test or run named in the Evidence column; **BLOCKED** = cannot
be demonstrated in my sandbox, needs your machine; **N/A** = deliberately not built. A file existing is not counted as proof.

Numbers (re-run just now in the sandbox): **240 tests passed** (unit 193, integration 39, e2e 8), **99% line coverage**
(1411 of 1421 statements; line coverage is not correctness), ~2,900 lines of application code, ~3,000 lines of tests.

## A. Acceptance matrix
| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | FastAPI + Pydantic app, validated config | PASS | `tests/unit/test_config.py`, `test_health.py`; bad alert rules / short token stop startup (`test_alerts`, `test_security`) |
| 2 | Bounded SQLite (cannot grow forever) | PASS | span-count cap + age retention + per-query timeout -> 503: `test_trace_store.py` |
| 3 | OpenTelemetry tracing, parent/child, W3C propagation | PASS | `test_tracing.py` (traceparent, error spans, no stack traces) |
| 4 | Prometheus metrics, bounded label cardinality | PASS | `test_metrics.py`, `test_security.py` (unmatched routes, method->OTHER, 100-target cap); e2e checks exact counter values |
| 5 | Structured JSON logs with trace/request correlation | PASS | `test_logging_setup.py`, `test_correlation.py`; e2e: every `request_error` log id matches a stored ERROR trace |
| 6 | Instrumentation SDK for AI ops (model, embedding, retrieval, rerank, tool, agent step) | PASS | `test_ai_telemetry.py` with fakes (no paid calls) |
| 7 | Ingestion from other processes | PASS | OTLP/HTTP receiver: `test_otlp_ingest.py` (15), real exporter over a real socket (`test_live_integrations.py`) |
| 8 | Cost tracking that never invents numbers | PASS | unknown model => unpriced not 0; missing tokens => unknown; Decimal math: `test_pricing.py`, `test_cost_accounting.py`, e2e unknown-model scenario |
| 9 | Error monitoring (origins, not ancestor spans) | PASS | `/api/errors`; e2e failure scenario: 10 failures => 10 errors |
| 10 | Dashboard pages (Overview, AI & cost, Traces, Errors, Alerts), empty/error states, light/dark, no innerHTML | PASS | `test_dashboard.py`; real Chromium run: 5 tabs render, 0 console/CSP errors, on-screen total matches API (coarse substring check, outside pytest) |
| 11 | Alerting with no flapping, no-data handling, persisted state, webhook | PASS | `test_alerts.py` (state machine, restart without re-notify, real local webhook); e2e lifecycle None->PENDING->FIRING->OK |
| 12 | Security: optional token, loopback default, redaction, CSP, header hardening | PASS (with stated limits) | `test_security.py` incl. leak canary across logs/spans/store/metrics/API; mutation check caught disabled redaction and unauthenticated ingest |
| 13 | Reliability: telemetry never breaks the app | PASS | exporters fail soft (`test_trace_store`, `test_alerts` dead webhook, observer hook failures) |
| 14 | Performance / retention | PASS (measured, no target available to me) | `scripts/bench_overhead.py`: ~0.9 ms/request overhead in my sandbox, **2.8 ms/request on your machine** (25% of a trivial handler). Indicative only; machine dependent; no load/soak test |
| 15 | Integration: Project 1 (production-rag) | PASS (with limit) | unmodified repo's real `init_otel`/`span()` shows up in obsdash; repo byte-identical, its 363 tests still pass. **Not verified:** a running production-rag server handling real `/ask` (needs OpenAI) |
| 16 | Integration: Project 2 (eval-harness) | PASS | `ObsdashRunObserver` driven by real `run_evaluation`; repo byte-identical, its 348 tests still pass (4 skipped) |
| 17 | Integration: Project 3 | N/A | project not built; `docs/integration_contract.md` only, no adapter claimed |
| 18 | Tests: unit + integration + e2e, success/slow/failure scenarios, alert transitions | PASS | 235 tests; mutation spot-check 7/8 caught, the 8th is an equivalent mutant (redundant guard) |
| 19 | No paid AI calls needed to test | PASS | all AI behaviour via fakes / MockSystem |
| 20 | Docker / local deployment | PASS | Run on the owner's Windows machine (Docker Desktop): image built, `docker compose ps` showed `Up ... (healthy)` on `127.0.0.1:8100->8100/tcp`, `scripts/smoke_test.py` with a token: **7/7 passed** (token rejection on API and ingest, OTLP trace visible, metrics, alerts). Also verified in the sandbox: compose config valid, clean-venv emulation. First attempt hit a port clash with a local uvicorn on 8100 (user-side, resolved by stopping it) |

## B. Engineering report
1. **Goal** — a real-time observability dashboard for AI systems that stays honest: unknown stays unknown.
2. **Architecture** — apps -> (OTel spans in-process, or OTLP/HTTP) -> redaction -> bounded SQLite store -> analytics (overview, errors, cost) -> JSON API -> static dashboard; Prometheus `/metrics` for obsdash's own process; alert evaluator reads the same store.
3. **Stack** — FastAPI, Pydantic v2, OpenTelemetry SDK/OTLP 1.45, prometheus-client, SQLite (WAL), static HTML/JS, uvicorn.
4. **Key decisions** — separate repo; no Tempo/Jaeger in v1; metrics for dashboards derived from stored spans (Prometheus has no in-process history); OTLP as the integration interface instead of a custom API; no built-in prices.
5. **Telemetry model** — request spans with route templates; AI operations as CLIENT spans (`gen_ai.*` + `obsdash.ai.*`); cost status known / unknown_model / no_usage.
6. **Cost accounting** — per-million-token pricing file you supply; Decimal arithmetic; never estimates tokens; unpriced calls counted and alertable.
7. **Alerting** — OK / PENDING / FIRING / NO_DATA; `for_seconds`, `min_samples`; a firing alert with no data stays FIRING; state persisted; webhook fails soft. Not Alertmanager.
8. **Security & privacy** — optional bearer/X-API-Key (constant-time, headers only), loopback default, best-effort pattern redaction on every export path, no stack traces, strict CSP by hash, bounded labels, request-id validation, bounded ingest. No users/roles/TLS/rate limiting.
9. **Integrations** — Project 1 via its existing OTLP export + ingest-time stage tagging (adds no tokens/cost); Project 2 via its `RunObserver`; Project 3 contract only.
10. **Testing** — see Section A, row 18; 99% line coverage; mutation spot-check; real-browser check.
11. **Performance** — about 0.9 ms (sandbox) to 2.8 ms (your machine) per request; BatchSpanProcessor keeps export off the request path.
12. **Retention & bounds** — 50,000 spans default, 168 h default, 5 s query timeout, 4 MiB / 2,000-span ingest caps.
13. **Deployment** — Dockerfile + one-service compose (host loopback publish, non-root, read-only FS, volume), plus plain `uvicorn`. Verified on the owner's machine (healthy, smoke 7/7).
14. **Defects found and fixed during the project** — wrong trace root on timestamp ties (Phase 11); cost-by-model page and AI-operations table disagreed on "unpriced" for failed calls without usage (found from a real dashboard screenshot after delivery, fixed, pinned by an e2e assertion); Docker's own health check was being counted as real traffic, which would have blocked the no-traffic and stale-telemetry alerts (found on the owner's Docker run; probe moved to an untracked `/healthz`, test added); the Docker `seed` service failed on the owner's machine because the runtime image lacked `httpx` and the seed script looked for its pricing example outside the image layout (both fixed; clicking a trace opened its detail BELOW the 30-row list, off-screen, so it looked like a dead click: the detail now opens at the top with a Close button, and the real-browser check now verifies a trace opens in view and closes; two tests now run the seed script from a replica of the image layout. Lesson: my earlier sandbox check of the seed command had not actually confirmed success); earlier phases' defects are recorded in their docs/phaseN.md.
15. **Known limitations** — traces only over OTLP/HTTP protobuf (no JSON/gRPC/logs/metrics ingest); `/metrics` covers only obsdash's own process; remote span names are trusted as sent; redaction is pattern-based (can miss novel secret formats); single-node SQLite, no load/soak or multi-writer testing; no HA; alerting is simple.
16. **Unverified items** — production-rag server under real traffic; behaviour on your Windows/conda setup beyond the 192-test run you reported for Phase 10.
17. **How to run** — `docs/phase13.md` (Docker and local); `python scripts/smoke_test.py URL [--token T]` verifies a live instance in seconds.
18. **Next steps if continued** — OTLP logs/metrics ingest, per-route scopes/TLS via reverse proxy, a real trace backend (Tempo) behind the same OTLP interface, load testing, dashboard drill-down links from alerts to traces.
