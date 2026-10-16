"""
Phase 10: scrub spans on their way OUT.

Redacting only inside our own SQLite store is not enough: the same spans
also go to the console exporter and to an OTLP Collector (possibly a third
party). Any instrumentation code -- ours or a library's -- can call
`span.set_attribute("db.statement", "... api_key=...")` and that raw value
would be exported untouched. So every exporter obsdash wires up is wrapped
in `RedactingSpanExporter`, which hands the inner exporter scrubbed COPIES
(span name, attributes, event attributes, status text).

Same limits as redaction.py: pattern-based, best effort.
"""

import re
from typing import Mapping, Sequence

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Status

from obsdash.redaction import REDACTED, redact_text

_SECRET_KEY = re.compile(
    r"authorization|password|passwd|secret|api[_-]?key|cookie|bearer|credential|private[_-]?key|access[_-]?key", re.I)
_MAX_ATTR_VALUE_CHARS = 256
_MAX_ATTRS = 50
_EXPORT_MAX_CHARS = 1024
_EXPORT_MAX_ATTRS = 128


def sanitize_attributes(attrs) -> dict:
    """For the STORE: flat JSON-able dict, strings redacted + truncated,
    secret-looking keys blanked, at most 50 entries."""
    out: dict = {}
    for i, (key, value) in enumerate((attrs or {}).items()):
        if i >= _MAX_ATTRS:
            break
        key = str(key)
        if _SECRET_KEY.search(key):
            out[key] = REDACTED
        elif isinstance(value, str):
            out[key] = redact_text(value, _MAX_ATTR_VALUE_CHARS)
        elif isinstance(value, (bool, int, float)) or value is None:
            out[key] = value
        else:
            out[key] = redact_text(str(value), _MAX_ATTR_VALUE_CHARS)
    return out


def _redact_value(value):
    if isinstance(value, str):
        return redact_text(value, _EXPORT_MAX_CHARS)
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _redact_value(v) for k, v in value.items()}
    return value  # numbers, bools, bytes: not scanned


def redact_attributes(attrs) -> dict:
    """For EXPORT: keeps value types (OTLP cares), scrubs strings."""
    out: dict = {}
    for i, (key, value) in enumerate((attrs or {}).items()):
        if i >= _EXPORT_MAX_ATTRS:
            break
        out[key] = REDACTED if _SECRET_KEY.search(str(key)) else _redact_value(value)
    return out


def redact_span(span: ReadableSpan) -> ReadableSpan:
    status = span.status
    if status is not None and status.description:
        status = Status(status.status_code, redact_text(status.description, 200))
    events: Sequence[Event] = [
        Event(e.name, redact_attributes(e.attributes), e.timestamp) for e in span.events
    ]
    return ReadableSpan(
        name=redact_text(span.name, 200),
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=redact_attributes(span.attributes),
        events=events,
        links=span.links,
        kind=span.kind,
        status=status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class RedactingSpanExporter(SpanExporter):
    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self.inner.export([redact_span(s) for s in spans])

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        flush = getattr(self.inner, "force_flush", None)
        return flush(timeout_millis) if flush else True
