# Phase 3 — OpenTelemetry tracing

## What was built
- `obsdash/otel_setup.py` — builds a TracerProvider (Resource: service.name, version, environment). Console exporter by default; OTLP/HTTP + BatchSpanProcessor when `OTEL_EXPORTER_OTLP_ENDPOINT` is set.
- `obsdash/tracing.py` — `request_span` (extracts W3C `traceparent`, SERVER span), `child_span`, `mark_span_ok/error`, `current_trace_and_span_ids`.
- `obsdash/middleware.py` — one span per request; attributes `http.method`, `http.route`, `http.status_code`, `obsdash.request_id`; status OK / ERROR; exception recorded on span.
- `obsdash/logging_setup.py` — `trace_id` / `span_id` log fields now real (were null in Phase 2).
- `obsdash/main.py` — `create_app(tracer_provider=None)`; `/health` has child span `check_service_identity`; provider shut down (flushed) on exit.

## Concepts
- **Span**: one timed operation. **Trace**: a tree of spans sharing a trace_id. **Parent/child**: child.parent_id = parent's span_id.
- **Context propagation**: `traceparent: 00-<trace_id>-<parent_span_id>-01` lets a caller's trace continue in this service.
- **SimpleSpanProcessor** exports immediately (dev); **BatchSpanProcessor** exports in the background (non-blocking).
- OTel's global provider can be set only once per process, so tests inject their own provider.

## Commands (Windows, conda env)
```
pip install -r requirements-dev.txt
python -m pytest -q
python -m uvicorn obsdash.main:app --port 8100
curl http://localhost:8100/health
```
Spans print to the server console as JSON.

## Results
27 passed (21 from Phases 1–2 + 6 new). Real-server check: propagated trace id appeared in both child and request spans and in the `request_complete` log line.

## Honest limits
- OTLP export is only tested for construction, not delivery (no Collector here).
- `request_start` is logged before the span opens, so its trace_id is null; later lines carry it.
- Metrics half of Phase 3 is deferred to Phase 4.
