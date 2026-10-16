# Phase 2 — Structured logging and correlation

## 1. What was implemented

Every log line this service emits is now one JSON object, carrying a
`request_id` that's the same across every log line produced while
handling one HTTP request — generated fresh per request, or honored
verbatim if the caller already supplied one via an `X-Request-ID`
header. An unhandled exception in any route is caught, logged with full
detail server-side, and turned into a generic `500` response carrying
only the `request_id` — never the real exception message or a stack
trace.

## 2. Files and their responsibilities

- `obsdash/correlation.py` — `new_request_id()`/`get_request_id()`/
  `set_request_id()`/`reset_request_id()`: the contextvar holding one
  request's id, readable from anywhere in that request's code path.
- `obsdash/logging_setup.py` — `JsonFormatter` (renders one `LogRecord`
  as one JSON line) + `CorrelationFilter` (attaches `request_id`/
  `trace_id`/`span_id` to every record automatically) +
  `configure_logging()` (wires both onto the root logger, called once
  from `main.py`'s `lifespan`).
- `obsdash/middleware.py` — `RequestContextMiddleware`: assigns/honors
  the request ID, logs `request_start`/`request_complete`/
  `request_error`, times the request, catches unhandled exceptions
  safely, sets the `X-Request-ID` response header.
- `tests/unit/conftest.py` — an autouse fixture resetting the
  request-id contextvar between tests (pytest's sync tests share one
  context, so without this, one test's request ID could leak into the
  next).

## 3. Concepts, explained

**Request ID vs. trace ID vs. span ID** — easy to conflate, worth being
precise about:
- A **request ID** identifies one HTTP request end-to-end, inside *this*
  one service.
- A **trace ID** identifies one logical operation across *potentially
  many* services — if this API called a second service, and that called
  a third, all three would share the same trace ID. Within one service
  with no downstream calls (today's state), a trace ID and request ID
  mean the same thing in practice.
- A **span ID** identifies *one operation* within a trace — a trace is a
  tree of spans, each with a parent reference.

This phase only has request IDs. `trace_id`/`span_id` are already
reserved fields in every log line (currently always `null`) so Phase 3
populates existing fields rather than changing the log shape.

**How logs relate to traces**: a log line is a point-in-time text/detail
event; a trace is a structured tree of timed operations. They become
powerful together specifically *because* they share an ID — "this log
line's error happened during this exact span of this exact trace" is
only answerable when both carry the same correlation id. That's the
entire reason this phase exists before Phase 3's real tracing: the
correlation discipline has to be in place first, or adding tracing later
just produces two uncorrelated signals side by side.

**Why a `logging.Filter` can "decorate" and not just filter**: despite
the name, `logging.Filter.filter()` can mutate the record it's given and
still return `True` — that's how `CorrelationFilter` attaches
`request_id` to every record without every call site having to pass it
explicitly.

## 4. Commands to run and test this phase

```bash
python -m pytest -v

python -m uvicorn obsdash.main:app --reload --port 8100
# in another terminal:
curl -i http://127.0.0.1:8100/health
curl -i http://127.0.0.1:8100/health -H "X-Request-ID: my-own-id-123"
```

Watch the first terminal (where uvicorn is running) while you curl —
you'll see one JSON line per log event, interleaved with uvicorn's own
plain-text access log lines (uvicorn logs those itself, separately from
`obsdash`'s own logger — expected, not something this phase changes).

## 5. Actual test results (just run)

```
21 passed in 1.39s
```
(6 from Phase 1 + 3 correlation + 7 logging + 5 middleware = 21.)

Also verified against the **real** running server (not just the test
client): sent one request with a caller-supplied `X-Request-ID: my-test-id-999`
and one without. Real response headers:
```
x-request-id: my-test-id-999        (honored exactly)
x-request-id: ec1a4d6e-af12-4a34-afba-6cd898d79ae8   (freshly generated)
```
Real log lines produced (both requests), confirming `request_id` matches
the header exactly, `trace_id`/`span_id` are `null`, and `duration_ms`
was captured:
```json
{"timestamp": "...", "severity": "INFO", "service": "obsdash", "environment": "development", "logger": "obsdash.request", "message": "request_start", "request_id": "my-test-id-999", "trace_id": null, "span_id": null, "operation": "GET /health"}
{"timestamp": "...", "severity": "INFO", "service": "obsdash", "environment": "development", "logger": "obsdash.request", "message": "request_complete", "request_id": "my-test-id-999", "trace_id": null, "span_id": null, "operation": "GET /health", "status_code": 200, "duration_ms": 1.98}
```

## 6. A question to check your understanding

If you send two requests back to back **without** an `X-Request-ID`
header on either one, will their `request_id` values be the same or
different — and why does that matter for debugging two different users'
requests that happen to hit the server at nearly the same time?

## 7. Waiting for your review before Phase 3 (OpenTelemetry instrumentation).
