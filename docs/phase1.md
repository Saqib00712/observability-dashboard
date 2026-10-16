# Phase 1 — Observability fundamentals and minimal skeleton

## 1. What was implemented

A minimal FastAPI application that starts, serves `GET /health`, and
shuts down cleanly — nothing about metrics/traces/logs yet (that's
Phases 2–4). Config is environment-driven via `pydantic-settings`, the
same pattern `production-rag/app/config.py` already uses.

## 2. Files and their responsibilities

- `obsdash/config.py` — `Settings` (a `pydantic-settings` `BaseSettings`
  subclass): every configurable value declared once, with its type and
  default, overridable by an env var of the same name (or a `.env`
  file). `get_settings()` is the cached accessor request handlers will
  use; `Settings()` directly is what tests use when they need a fresh
  read after changing env vars.
- `obsdash/main.py` — `create_app()`: a factory (not a bare module-level
  `app = FastAPI()`) so tests can build a fresh app per test once later
  phases need per-test config. `lifespan` is an explicit no-op context
  manager — Phase 3's OTel init call lands there.
- `tests/unit/test_config.py` / `test_health.py` — prove defaults are
  sane, env vars actually override them, and a real HTTP request through
  FastAPI's routing gets the right status/shape (not just "the module
  imports without error").

## 3. Concepts, explained

**Metrics, traces, and logs** are the three classic "pillars" of
observability, and they answer different questions:
- A **metric** is a number that changes over time — "47 requests in the
  last minute," "p95 latency is 230ms." Cheap to store, cheap to query,
  but it can't tell you *which* request was slow or *why*.
- A **trace** is the record of one specific request's journey through
  your system — a tree of **spans** (one span per operation: "embed the
  query," "call the LLM," "validate citations"), each with a start time,
  duration, and status. A trace answers "what happened during *this*
  one request" — the thing a metric structurally cannot answer.
- A **log** is a timestamped text/structured event — "query embedding
  failed: rate limited." Logs carry detail traces and metrics don't
  (a full error message, a stack trace) but aren't aggregatable the way
  metrics are and aren't inherently structured as a tree the way traces
  are.

All three get far more useful when they share **correlation IDs** —
Phase 2's subject.

**Prometheus** is a metrics *database* with a pull model: your
application just exposes current counter/histogram values at a
`/metrics` HTTP endpoint (in a specific text format), and a separate
Prometheus *server* process scrapes that endpoint on a schedule (e.g.
every 15s) and stores the time series itself. Your app never pushes
metrics anywhere or talks to Prometheus directly — it just always has
current numbers ready to be read. `production-rag` already does exactly
this today.

**OpenTelemetry (OTel)** is a vendor-neutral standard (an API + SDK +
wire protocol called OTLP) for producing traces, metrics, and logs, so
your instrumentation code doesn't have to be rewritten per observability
backend. You instrument once with the OTel API; where the data actually
*goes* (console, a file, a real backend) is a separate exporter
configuration decision, changeable without touching instrumented code.

**Grafana** is a dashboard/visualization tool that queries *other*
systems (Prometheus being the classic one) and renders charts — it
stores no telemetry itself, just dashboard definitions and queries.

None of these four exist as running infrastructure yet in Phase 1 — this
phase is deliberately just the application skeleton they'll eventually
plug into.

## 4. Commands to run and test this phase

```bash
cd observability-dashboard
pip install -r requirements-dev.txt

# Run the tests
python3 -m pytest -v

# Run the real server (separate terminal) and hit it for real
python3 -m uvicorn obsdash.main:app --reload --port 8100
curl http://127.0.0.1:8100/health
```

## 5. Actual test results (just run, not estimated)

```
tests/unit/test_config.py::test_defaults_are_set_without_any_env_vars PASSED
tests/unit/test_config.py::test_environment_variable_overrides_default PASSED
tests/unit/test_config.py::test_get_settings_is_cached PASSED
tests/unit/test_health.py::test_health_returns_ok_status PASSED
tests/unit/test_health.py::test_health_response_shape PASSED
tests/unit/test_health.py::test_unknown_route_returns_404 PASSED

6 passed in 0.57s
```

Also manually verified: started the *real* server process with
`uvicorn` (not just FastAPI's in-process `TestClient`), hit
`GET /health` with `curl`, got `{"status":"ok","service":"obsdash",...}`
back, then shut it down cleanly (`Application shutdown complete` in the
log, no errors).

## 6. A question to check your understanding

If a request to this service is slow, which of metrics/traces/logs would
tell you it happened at all (vs. being normal), which would tell you
*which specific operation* inside that one request was slow, and which
would give you a human-readable error message if one occurred? (No code
needed to answer — this is about the three pillars, not this app yet.)

## 7. Waiting for your review before Phase 2.
