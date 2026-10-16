"""
Phase 3: span-starting helpers used by the middleware and by routes that
want a child span around one specific operation.

`request_span()` is where trace CONTEXT PROPAGATION happens: it extracts
any incoming W3C `traceparent` header via OpenTelemetry's standard
propagator before starting the request's span. If a caller already has a
trace in progress and sends it along, our span becomes a CHILD of that
existing trace instead of starting a brand new one -- this is the whole
mechanism that lets one logical operation's trace span multiple
services. Today nothing calls INTO this service with a real upstream
trace (no caller instruments outbound calls that way yet), so this path
is real and tested, but exercised in tests via a synthetic header, not
an actual upstream service.
"""

from contextlib import contextmanager
from typing import Iterator, Mapping

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.trace import Span, SpanKind, StatusCode
from opentelemetry.trace import Status as SpanStatus

from obsdash.redaction import redact_text


def get_tracer(tracer_provider: trace.TracerProvider) -> trace.Tracer:
    return tracer_provider.get_tracer("obsdash")


@contextmanager
def request_span(
    tracer: trace.Tracer, name: str, headers: Mapping[str, str], attributes: dict
) -> Iterator[Span]:
    """Starts the span for one incoming HTTP request, as a child of
    whatever trace context (if any) was propagated in via `headers`
    (checked for a W3C `traceparent` by `propagate.extract`). Sets
    `attributes` immediately (method, route, etc. -- known before the
    handler runs); the caller is responsible for recording the status
    code / exception once the response is known, same as `middleware.py`
    already separates "log the start" from "log the end."
    """
    parent_context = propagate.extract(headers)
    with tracer.start_as_current_span(
        name, context=parent_context, kind=SpanKind.SERVER, attributes=attributes
    ) as span:
        yield span


@contextmanager
def child_span(tracer: trace.Tracer, name: str, attributes: dict | None = None) -> Iterator[Span]:
    """A span for one operation WITHIN an already-active request span --
    automatically becomes a child of whatever span is currently active
    (OTel tracks this via its own context, not anything obsdash manages).
    This is the primitive Phase 5 will reuse for AI-specific spans
    (embedding, retrieval, generation, ...); Phase 3 uses it once, inside
    `/health`, to prove the parent-child mechanism works end to end
    before anything AI-specific depends on it.
    """
    with tracer.start_as_current_span(name, attributes=attributes or {}) as span:
        yield span


def mark_span_ok(span: Span) -> None:
    span.set_status(SpanStatus(StatusCode.OK))


def mark_span_error(span: Span, exc: BaseException) -> None:
    """Records the exception type + redacted message ON THE SPAN (server-side telemetry, same
    trust level as a log line -- never returned to the client) and sets
    span status to ERROR. Mirrors `middleware.py`'s own
    log-full-detail-server-side / return-generic-detail-to-client split,
    applied to tracing instead of logging."""
    # Deliberately NOT span.record_exception(): that attaches the full stack
    # trace and raw message, which exporters would ship off-box unredacted.
    # We record the exception TYPE and a redacted, truncated message only.
    span.add_event("exception", {
        "exception.type": type(exc).__qualname__,
        "exception.message": redact_text(str(exc), 200),
    })
    span.set_status(SpanStatus(StatusCode.ERROR, description=type(exc).__name__))


def current_trace_and_span_ids() -> tuple[str | None, str | None]:
    """The real, active OTel trace/span IDs, formatted as the standard
    lowercase hex strings (32 hex chars for a trace id, 16 for a span
    id) -- or `(None, None)` if no span is currently recording (e.g. a
    log line emitted outside any request, or tracing produced a
    non-recording span). This is what `logging_setup.py`'s
    `CorrelationFilter` calls so Phase 2's reserved `trace_id`/`span_id`
    log fields go from always-`null` to the real thing, now that real
    tracing exists."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return None, None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


def inject_trace_headers(headers: dict | None = None) -> dict:
    """OUTBOUND propagation: add the current trace context (W3C
    `traceparent`) to the headers of a call this service makes to another
    one, so the callee's spans join THIS trace. The mirror image of
    `request_span`, which handles the inbound side."""
    carrier = dict(headers or {})
    propagate.inject(carrier)
    return carrier
