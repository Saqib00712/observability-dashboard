# observability-dashboard

A production-grade observability platform for AI systems — collects traces via OTLP, scrapes Prometheus metrics, computes latency percentiles, tracks AI token usage and cost, fires alerts, and serves a live browser dashboard. Built as Project 4 of a 43-project AI Engineering curriculum.

Integrates with **production-rag** (Project 1) over OTLP/HTTP and **eval-harness** (Project 2) via a pluggable observer — zero code changes required in either project.

---

## What This Project Does

Most AI systems ship with scattered logs and no unified view of what's happening. This platform gives you one place to see request throughput, error rates, latency percentiles, model call costs, trace waterfalls, and firing alerts — for both your RAG pipeline and your eval runs.

```
production-rag  ──OTLP/HTTP──▶┐
                               │
eval-harness ──RunObserver──▶  obsdash (FastAPI)
                               │
Any app with OTLP export ────▶ │
                               ▼
                         SQLite trace store
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
        /dashboard        /api/traces     /metrics
     (live browser UI)  (trace API)  (Prometheus scrape)
              │
    ┌─────────┼──────────┬──────────┬──────────┐
    ▼         ▼          ▼          ▼          ▼
 Overview   AI & Cost  Traces    Errors    Alerts
```

---

## Built Phase by Phase — 13 Phases

| Phase | What Was Built |
|-------|---------------|
| 1 | FastAPI skeleton — `GET /health`, env-driven config via pydantic-settings |
| 2 | Structured JSON logging, per-request correlation IDs, `X-Request-ID` header, safe 500 handler |
| 3 | OpenTelemetry instrumentation — TracerProvider, per-request SERVER spans, W3C `traceparent` propagation, child spans |
| 4 | Prometheus metrics — `requests_total`, `request_duration_seconds` histogram, `requests_in_flight` gauge; `GET /metrics` |
| 5 | AI-specific telemetry — `AITelemetry.operation()` context manager for MODEL_CALL, EMBEDDING, RETRIEVAL, RERANK, TOOL_CALL, AGENT_STEP; AI metrics counters + duration histograms + token counters |
| 6 | Distributed trace storage — bounded SQLite span store, `GET /api/traces` + `GET /api/traces/{trace_id}` (nested waterfall), outbound trace header injection |
| 7 | Cost and token accounting — pluggable pricing table (JSON), exact Decimal math, unknown-model tracking, `GET /api/costs/summary` |
| 8 | Browser dashboard — Overview, AI & Cost, Traces, Errors pages; `GET /api/overview`, `GET /api/errors`; seed script for demo data |
| 9 | Alerting — rule engine (OK/PENDING/FIRING/NO_DATA states), SQLite alert history, webhook notifications, Alerts dashboard tab, Prometheus alert metrics |
| 10 | Security hardening — optional API token auth, secrets redaction in logs/spans/store/export, cardinality caps, response security headers, strict CSP, retention pruning, query timeouts |
| 11 | Integrations — `POST /v1/traces` OTLP/HTTP receiver; production-rag span enricher; eval-harness `ObsdashRunObserver`; Project 3 adapter contract |
| 12 | Test suite — 229 tests: unit (193) + integration (29) + E2E (7); 99% line coverage; mutation spot-check (7/8 bugs caught) |
| 13 | Docker deployment — Dockerfile, docker-compose, persistent volume, non-root user, read-only FS, healthcheck, smoke test script |

**Final: 229 tests — 99% line coverage.**

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                   OBSERVABILITY DASHBOARD                       │
├─────────────────────────────────────────────────────────────────┤
│  Ingest                                                         │
│  ├── POST /v1/traces — OTLP/HTTP protobuf receiver             │
│  │     Token-protected, body-capped, redacted before storing    │
│  └── ObsdashRunObserver — eval-harness RunObserver plugin       │
│                                                                 │
│  Instrumentation                                                │
│  ├── Per-request SERVER spans (W3C traceparent propagation)     │
│  ├── AI operation spans — model call, embedding, retrieval,     │
│  │   rerank, tool call, agent step                              │
│  └── Prometheus counters + histograms + gauges                  │
│                                                                 │
│  Storage                                                        │
│  └── SQLite — spans (bounded 50k, 7-day retention),            │
│               alert state + history                             │
│                                                                 │
│  APIs                                                           │
│  ├── GET /api/overview — requests, error rate, p50/p95/p99      │
│  ├── GET /api/traces + /{trace_id} — trace list + waterfall     │
│  ├── GET /api/costs/summary — tokens, cost, unpriced warning    │
│  ├── GET /api/errors — grouped errors + origin traces           │
│  └── GET /api/alerts — rule states + recent transitions         │
│                                                                 │
│  Dashboard (static HTML/JS, no build step)                      │
│  ├── Overview — throughput, error rate, latency charts          │
│  ├── AI & Cost — tokens, cost, per-model tables                 │
│  ├── Traces — list, error filter, span waterfall + attributes   │
│  ├── Errors — grouped, click opens trace                        │
│  └── Alerts — firing count badge, rule states, transitions      │
│                                                                 │
│  Alert Engine (background thread)                               │
│  ├── Metrics: error_rate, latency_p95, ai_cost, staleness, ...  │
│  ├── States: OK → PENDING → FIRING → RESOLVED                   │
│  └── Webhook notifications (one-shot, fail-open)                │
│                                                                 │
│  Security                                                       │
│  ├── Optional Bearer/X-API-Key token (constant-time compare)    │
│  ├── Secrets redacted in logs, spans, store, OTLP export        │
│  ├── Strict CSP (SHA-256 script hashes), security headers       │
│  └── Cardinality caps, query timeouts, bounded export queue     │
│                                                                 │
│  Infrastructure                                                 │
│  ├── Docker — non-root, read-only FS, persistent /data volume   │
│  └── GET /metrics — Prometheus scrape endpoint                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Dashboard Pages

| Page | Shows |
|------|-------|
| **Overview** | Total requests, error rate, p50/p95/p99 latency, model calls, estimated cost; requests-per-bucket chart, p95 trend, per-route table |
| **AI & Cost** | Total tokens (input/output), total cost, per-operation table, per-model table, unpriced-calls warning |
| **Traces** | Trace list (filterable to errors only), span waterfall with attributes and durations for any selected trace |
| **Errors** | Errors grouped by type, recent error list — click any to open its full trace |
| **Alerts** | All rules with current state (OK/PENDING/FIRING/NO_DATA), recent state transitions, firing count badge |

All data is computed from real spans in the SQLite store — no hardcoded values. Empty store shows a "No telemetry yet" message. Auto-refreshes every 5 seconds.

---

## Alert States

```
          breach starts
OK ──────────────────▶ PENDING
▲                          │ breach lasts for_seconds
│    breach ends            ▼
└──────────────────── FIRING ──▶ webhook notification
                           │ breach ends
                           ▼
                       RESOLVED ──▶ webhook notification
```

`NO_DATA` — metric cannot be computed (too few samples or no telemetry). A FIRING alert that loses data stays FIRING — recovery is never assumed without evidence.

---

## Tech Stack

![Python](https://img.shields.io/badge/Python-3.10+-blue?style=flat-square)
![FastAPI](https://img.shields.io/badge/FastAPI-Backend-green?style=flat-square)
![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-Tracing-purple?style=flat-square)
![Prometheus](https://img.shields.io/badge/Prometheus-Metrics-orange?style=flat-square)
![SQLite](https://img.shields.io/badge/SQLite-Storage-blue?style=flat-square)
![Docker](https://img.shields.io/badge/Docker-Container-blue?style=flat-square)

- **FastAPI** — async REST API + static dashboard serving
- **OpenTelemetry SDK** — TracerProvider, OTLP/HTTP exporter, W3C context propagation
- **prometheus-client** — counter/histogram/gauge, `/metrics` text format
- **pydantic-settings** — environment-driven config
- **SQLite** — span store, alert state, bounded by row count + age
- **Docker + docker-compose** — non-root, read-only FS, persistent volume, healthcheck
- **Vanilla HTML/JS** — zero-dependency dashboard (no build step, works offline)

---

## Project Structure

```
observability-dashboard/
│
├── obsdash/
│   ├── main.py                        # create_app() factory, lifespan, routes
│   ├── config.py                      # Settings (pydantic-settings, SecretStr for tokens)
│   ├── correlation.py                 # request_id contextvar
│   ├── logging_setup.py               # JsonFormatter, CorrelationFilter, configure_logging()
│   ├── middleware.py                  # RequestContextMiddleware — ID, metrics, spans, headers
│   ├── tracing.py                     # request_span, child_span, mark_ok/error, propagation
│   ├── otel_setup.py                  # TracerProvider, console/OTLP exporters, RedactingExporter
│   ├── metrics.py                     # Prometheus Metrics class (own registry)
│   ├── ai_telemetry.py                # AITelemetry.operation() context manager
│   ├── trace_store.py                 # TraceStore — bounded SQLite span store + exporter
│   ├── pricing.py                     # PricingTable — token → USD (Decimal math)
│   ├── analytics.py                   # Percentiles, error grouping, window math
│   ├── alert_rules.py                 # Rule model, strict validation
│   ├── alert_engine.py                # State machine, SQLite history, webhook notifier
│   ├── redaction.py                   # Pattern-based secrets redaction (logs + text)
│   ├── span_redaction.py              # RedactingSpanExporter (wraps any exporter)
│   ├── auth.py                        # Bearer/X-API-Key constant-time token check
│   ├── otlp_ingest.py                 # POST /v1/traces OTLP/HTTP protobuf receiver
│   ├── api_traces.py                  # GET /api/traces + /{trace_id}
│   ├── api_costs.py                   # GET /api/costs/summary
│   ├── api_dashboard.py               # GET /api/overview + /api/errors
│   ├── api_alerts.py                  # GET /api/alerts
│   │
│   ├── integrations/
│   │   ├── production_rag.py          # Enriches production-rag AI stage spans at ingest
│   │   └── eval_harness_observer.py   # ObsdashRunObserver — plugs into eval-harness
│   │
│   └── static/
│       └── dashboard.html             # Full browser dashboard (no CDN, works offline)
│
├── scripts/
│   ├── seed_demo_data.py              # Push synthetic traffic through real instrumentation
│   ├── smoke_test.py                  # 7-check smoke test (CI + Docker verification)
│   ├── bench_overhead.py              # Middleware overhead benchmark
│   ├── demo_alerts.py                 # Alert lifecycle demo (simulated clock)
│   ├── example_production_rag_to_obsdash.py
│   └── example_eval_to_obsdash.py
│
├── tests/
│   ├── unit/                          # 193 tests — one module per component
│   ├── integration/                   # 29 tests — OTLP wire, real OTel export, sibling repos
│   └── e2e/                           # 7 scenarios — full pipeline, no paid AI calls
│
├── config/
│   ├── alert_rules.example.json
│   └── pricing.example.json
│
├── docs/
│   └── integration_contract.md        # Project 3 adapter contract
│
├── Dockerfile                         # Non-root, read-only FS, multi-stage
├── docker-compose.yml                 # Persistent volume, healthcheck, seed profile
├── .env.example
├── requirements.txt
└── requirements-dev.txt
```

---

## Getting Started

### With Docker (recommended)

```bash
git clone https://github.com/Saqib00712/observability-dashboard.git
cd observability-dashboard
cp .env.example .env
# edit .env — set API_TOKEN (recommended)
docker compose up --build -d
docker compose ps          # wait for STATUS: healthy (~30s)
python scripts/smoke_test.py http://127.0.0.1:8100 --token YOUR_TOKEN
# open http://127.0.0.1:8100/dashboard
```

Seed demo data:
```bash
docker compose --profile demo run --rm seed
```

### Without Docker

```bash
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export API_TOKEN=your-token-here   # optional
uvicorn obsdash.main:app --port 8100
# open http://127.0.0.1:8100/dashboard
```

### Run tests

```bash
pip install -r requirements-dev.txt
pytest -q
# 229 passed
```

---

## Connecting Your Apps

### production-rag (Project 1) — zero code changes

Add to production-rag's `.env`:
```
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8100/v1/traces
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer YOUR_TOKEN
```
If production-rag runs in a container: use `http://host.docker.internal:8100/v1/traces`.

### eval-harness (Project 2) — one new class, no changes to runner.py

```python
from obsdash.integrations.eval_harness_observer import ObsdashRunObserver

observer = ObsdashRunObserver(base_url="http://127.0.0.1:8100", api_token="YOUR_TOKEN")
results = run_evaluation(dataset=ds, system=sys, evaluators=[...], observer=observer)
```

### Any other system with OTLP export

Point your OTLP exporter at `http://127.0.0.1:8100/v1/traces` with a `Bearer` token header. Any span lands in the dashboard automatically.

---

## Alert Rule Example

```json
{
  "id": "high-error-rate",
  "metric": "request_error_rate",
  "comparator": ">",
  "threshold": 0.05,
  "window_minutes": 5,
  "for_seconds": 60,
  "min_samples": 10,
  "severity": "critical"
}
```

Set `ALERT_RULES_PATH=config/alert_rules.example.json` and `ALERT_WEBHOOK_URL=https://your-webhook` to enable.

Available metrics: `request_count`, `request_error_rate`, `request_latency_p95_ms`, `request_latency_p99_ms`, `ai_error_rate`, `ai_unpriced_calls`, `ai_cost`, `telemetry_staleness_seconds`.

---

## Key Concepts Covered

- **Three pillars of observability** — metrics (what), traces (which request), logs (why) and how they connect via correlation IDs
- **OpenTelemetry** — vendor-neutral instrumentation: instrument once, change backends without touching code
- **Prometheus pull model** — app exposes `/metrics`, Prometheus scrapes on a schedule
- **Distributed tracing** — W3C `traceparent` propagation so spans from multiple services share one trace ID
- **Cardinality** — why route templates (not raw paths) are used as labels; bounded label sets prevent metric explosion
- **AI-specific telemetry** — named span kinds (MODEL_CALL, EMBEDDING, RETRIEVAL, RERANK) following OpenTelemetry GenAI conventions
- **Cost honesty** — unknown model = unpriced (never zero); no usage reported = unknown (never estimated)
- **Secrets redaction in depth** — redact in logs → redact again on spans → redact again at export; defence in depth
- **Alert state machine** — PENDING buffer prevents transient spikes from firing; FIRING stays on data loss (never assume recovery)
- **Fail-open design** — every telemetry component degrades gracefully; a broken exporter never breaks the app

---

## Security Notes

- Default host is `127.0.0.1` — telemetry data is operationally sensitive; external exposure is an explicit opt-in
- Set `API_TOKEN` (16+ chars) to protect all `/api/*` and `/metrics` routes
- `/health` and the dashboard shell stay open (no token needed to see if the service is up)
- The dashboard stores the token in `sessionStorage` only (cleared on tab close); strict CSP prevents script injection
- Secrets (API keys, bearer tokens, JWTs, passwords, card numbers, emails) are redacted before logging, storing, or exporting

---

## Related Certifications

Built applying skills from the IBM **AI Observability and Monitoring**, **RAG for Generative AI Applications Specialization**, and **Building AI Agents and Agentic Workflows Specialization** on Coursera.

[![IBM Badge](https://img.shields.io/badge/IBM-RAG%20Specialization-blue?style=flat-square)](https://www.credly.com/users/muhammad-saqib.361f9b8c)
[![IBM Badge](https://img.shields.io/badge/IBM-AI%20Agents%20Specialization-blue?style=flat-square)](https://www.credly.com/users/muhammad-saqib.361f9b8c)

---

## Author

**Muhammad Saqib**
- GitHub: [@Saqib00712](https://github.com/Saqib00712)
- LinkedIn: [muhammad-saqib](https://www.linkedin.com/in/muhammad-saqib-68b9b3374/)
- Email: saqibkhosa649@gmail.com
- Credly: [15x IBM Certified](https://www.credly.com/users/muhammad-saqib.361f9b8c)
