# Phase 0 — Repository inspection and architecture

See the chat for the full write-up (reproduced here so it lives with the
project, not just in conversation history).

## Baseline found

- `production-rag`: Prometheus counters/histograms on `/metrics`, an
  in-process p50/p95/p99 recorder, per-stage cost tracking, contextvar
  tracing with a real-but-optional OTel SDK export
  (`requirements-otel.txt`, fail-open), `/observability/summary` + an
  HTML dashboard. Docker Compose exists (one `api` service).
- `eval-harness`: no live telemetry. `RunObserver` (6 lifecycle hooks) is
  the only extension point — nothing implements it today beyond logging
  and the static Day 25 dashboard.
- Project 3: does not exist in this environment — skipped per explicit
  instruction. Phase 11's Project 3 integration is a documented adapter
  contract only, never implementation against real code.
- Environment limit: this sandbox has the `docker` CLI but no running
  daemon, and the network allowlist doesn't include Docker Hub. Compose
  files can be written and syntax-validated (`docker compose config`)
  here; actually running the stack (Phase 13) is real work for you to
  run and report back on.

## Decisions (confirmed)

1. **Separate repo** — `observability-dashboard`, same convention as
   `eval-harness` never importing `production-rag`. This project never
   imports either of them; it talks to Project 1 over HTTP/OTLP and to
   Project 2 via a thin observer plugged into its existing `RunObserver`.
2. **Frontend**: static HTML/JS served by `dashboard-api` itself, no
   separate container — same no-build-step convention as eval-harness's
   Day 25 dashboard. (Recommended, not objected to.)
3. **No dedicated trace-storage backend in v1** (no Tempo/Jaeger) — the
   Collector + Prometheus + correlation IDs + Project 1's existing
   per-request span recorder covers trace inspection without a second
   storage system to run. Documented as a v1 boundary, not hidden.
   (Recommended, not objected to.)

## Reuse plan (smallest-change principle)

- Project 1 needs **zero code changes** for metrics (`/metrics` is
  already Prometheus-scrapeable) or tracing (its OTel export already
  exists — a config change points it at the new Collector).
- Project 2 gets exactly one new class, `ObsRunObserver`, implementing
  its existing `RunObserver` Protocol — no change to `runner.py` or any
  evaluator.
- `dashboard-api`'s SQLite is scoped to what Prometheus/OTel can't hold
  well (alert state/history, a bounded request index) — never a second
  metrics store.

## Phase mapping

Phases 1–4 build this new service's own skeleton/logging/tracing/metrics
(nothing to reuse — it's new). Phase 5 is the AI-specific instrumentation
layer Projects 1/2 will eventually call into. Phases 6–10 build the trace
story, cost accounting, the dashboard itself, alerting, hardening. Phase
11 is where the reuse plan above actually gets wired up. 12–14 are
verification and the final report.
