# Phase 6 — Distributed tracing and trace backend

## What was built
- `obsdash/trace_store.py` — bounded SQLite store of finished spans (`TraceStore`) + `TraceStoreExporter`. Max `TRACE_MAX_SPANS` (default 50,000); oldest deleted first. Secret-looking attribute keys redacted, values truncated to 256 chars, max 50 attributes. Opens lazily. Self-traffic (`/metrics`, `/api/traces`) is not stored.
- `obsdash/api_traces.py` — `GET /api/traces?limit=&status=error` (summaries) and `GET /api/traces/{trace_id}` (nested span tree with durations, status, attributes). trace_id validated as 32 lowercase hex (400 otherwise, 404 if unknown).
- `obsdash/tracing.py` — `inject_trace_headers()`: outbound propagation, the mirror of Phase 3's inbound extraction.
- `obsdash/otel_setup.py` — the real provider also feeds the store through a BatchSpanProcessor (background thread, never blocks a request).
- Config: `SQLITE_PATH` (default `obsdash.db`), `TRACE_MAX_SPANS`.

## Concepts
- **Distributed trace**: spans from several services sharing one trace_id. Service A injects `traceparent` into its outgoing call; service B extracts it, so B's span becomes A's child.
- **Trace backend**: where finished spans are kept for querying. Here: SQLite. A real deployment would use Jaeger/Tempo via the Collector (OTLP path from Phase 3).
- **Root span**: a span whose parent isn't in the store (remote caller or pruned) is shown as a root.
- Exporter failures return FAILURE but never raise: telemetry must not break the app.

## Commands
```
python -m pytest -q
set SQLITE_PATH=trace_demo.db            (Windows cmd; PowerShell: $env:SQLITE_PATH="trace_demo.db")
python -m uvicorn obsdash.main:app --port 8100
curl http://localhost:8100/health
curl http://localhost:8100/api/traces          (wait ~5 s: spans are batched)
curl http://localhost:8100/api/traces/<trace_id from the list>
```

## Results
54 passed (43 + 11 new). Live check: a request with a `traceparent` header was stored under that exact trace_id, as GET /health -> check_service_identity.

## Honest limits
- Cross-service test runs two apps in ONE process; it proves propagation mechanics, not a real network hop.
- Spans appear in the API after the batch flush (~5 s) while running live; tests use immediate export.
- SQLite is single-node and not a scalable trace backend. No sampling yet (every request is stored, bounded by the cap).
- Auto-instrumentation of outbound HTTP clients is not done: callers use `inject_trace_headers()`.
- No authentication on `/api/traces` until Phase 10.
- Pruning counts rows on every write; fine at this scale, not at millions.
