"""
Phase 2: structured (JSON) logging, one JSON object per line.

Why JSON lines instead of human-formatted text: a text log like
"2026-10-14 09:00:01 INFO Request completed in 45ms" is easy for a
PERSON to read once, and painful for a MACHINE to query ("show me every
request over 1000ms" means regex-parsing free text). A JSON line per
event is the reverse trade -- slightly less pleasant to eyeball raw, but
trivially filterable/aggregatable by any log pipeline (this is also
exactly what `StructuredLogObserver` in eval-harness's
`observability.py` already does with stdlib `logging`'s `extra=` kwarg,
same idea applied here to HTTP requests instead of eval runs).

Every log line gets the same baseline fields (timestamp, severity,
service, environment, request_id, trace_id, span_id) via `CorrelationFilter`
below -- a `logging.Filter` subclass, which despite the name is really a
"mutate every record passing through" hook, not just a yes/no gate. This
is where request-scoped data (Phase 2's `correlation.py`) gets attached
to EVERY log record automatically, so a route handler's own
`logger.info("something happened")` doesn't have to remember to pass
`request_id=...` by hand every single call site -- forgetting it once is
how correlation gaps happen in real systems.
"""

import json
import logging
import sys
from datetime import datetime, timezone

from obsdash.config import get_settings
from obsdash.correlation import get_request_id
from obsdash.redaction import redact_text
from obsdash.tracing import current_trace_and_span_ids

# Fields CorrelationFilter/JsonFormatter manage themselves -- never
# treated as "extra" caller-supplied fields even if a record happens to
# carry an attribute with the same name, since stdlib LogRecord already
# uses some of these names for its own purposes (e.g. `msg`).
_RESERVED_RECORD_ATTRS = frozenset(logging.LogRecord(
    "", 0, "", 0, "", (), None
).__dict__.keys()) | {"message", "asctime"}


class CorrelationFilter(logging.Filter):
    """Attaches request_id (and reserved trace_id/span_id -- see
    correlation.py's docstring) to every record that passes through a
    handler this filter is installed on. A `logging.Filter` returning
    `True` always (never actually filters anything out) is a
    recognized, intentional pattern for "decorate, don't gate" --
    the same role `production-rag`'s own middleware plays for its
    request-scoped logging."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        record.trace_id, record.span_id = current_trace_and_span_ids()
        return True


class JsonFormatter(logging.Formatter):
    """Renders one LogRecord as one JSON object. Sensitive-value
    handling (Phase 2's "safe error handling" requirement): this
    formatter never logs raw exception `args` or arbitrary object
    `repr()`s that might contain user input/secrets -- only the
    exception's type name and `str()` message, same restraint
    `production-rag`'s routers already apply before returning error
    detail to a CLIENT, applied here to what gets written to disk/stdout
    instead."""

    def format(self, record: logging.LogRecord) -> str:
        settings = get_settings()
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "severity": record.levelname,
            "service": settings.service_name,
            "environment": settings.environment,
            "logger": record.name,
            "message": redact_text(record.getMessage(), 2000),
            "request_id": getattr(record, "request_id", None),
            "trace_id": getattr(record, "trace_id", None),
            "span_id": getattr(record, "span_id", None),
        }

        if record.exc_info:
            exc_type, exc_value, _ = record.exc_info
            payload["error_category"] = exc_type.__name__ if exc_type else None
            payload["error_message"] = redact_text(str(exc_value), 500) if exc_value else None

        # Anything a caller passed via `extra={...}` that isn't one of
        # stdlib's own LogRecord attributes rides along too -- e.g.
        # `logger.info("request_complete", extra={"duration_ms": 42,
        # "operation": "GET /health"})`.
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_ATTRS and key not in payload:
                # Phase 10: string extras (paths, operation names, ...) are scrubbed too.
                payload[key] = redact_text(value, 500) if isinstance(value, str) else value

        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Call once, at process start (`main.py`'s `lifespan`). Idempotent:
    clears any handlers a previous call (or a test) already attached to
    the root logger, so calling this twice in a test session doesn't
    double-emit every line."""
    settings = get_settings()
    root = logging.getLogger()
    root.setLevel(settings.log_level)
    root.handlers.clear()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(CorrelationFilter())
    root.addHandler(handler)
