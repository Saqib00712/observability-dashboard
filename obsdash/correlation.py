"""
Phase 2: request correlation -- ONE id generated per incoming HTTP
request, readable from anywhere in that request's code path (a route
handler, a service function three calls deep) without threading it
through every function signature by hand.

Same primitive `production-rag/app/observability/tracing.py` already
uses for exactly this reason (see its own docstring): a `contextvar` is
per-async-task state that flows automatically through `await` calls in
the SAME task. No global mutable state, no manual parameter passing.

Request ID vs. trace ID vs. span ID -- the distinction Phase 2 asks to
explain, and worth being precise about because they're easy to conflate:

- A REQUEST ID identifies one HTTP request end-to-end, inside THIS one
  service. It's what lets you grep every log line for "everything that
  happened while handling this one call."
- A TRACE ID identifies one logical operation across POTENTIALLY MANY
  services -- if this dashboard's API called another service, and that
  service called a third, all three would share the same trace ID (that's
  what makes it "distributed" tracing). Within a single service with no
  downstream calls, a trace ID and a request ID often end up meaning the
  same thing in practice -- which is exactly the situation this phase is
  in, and exactly why Phase 3 doesn't need to throw this module away, just
  extend it.
- A SPAN ID identifies ONE OPERATION within a trace (e.g. "validate the
  request," "query the database") -- a trace is a tree of spans, each
  with its own span ID and a reference to its parent span's ID.

Phase 2 only has request IDs -- real spans/trace propagation don't exist
until Phase 3's OTel instrumentation. The log schema below already
reserves `trace_id`/`span_id` fields (always `null` for now) so adding
real tracing later is Phase 3 populating existing fields, not changing
the log shape out from under anything that already depends on it.
"""

import uuid
from contextvars import ContextVar

_request_id: ContextVar[str | None] = ContextVar("_request_id", default=None)


def new_request_id() -> str:
    """A fresh, unpredictable request id. Not a sequential/incrementing
    id on purpose -- a predictable id in a response header or log line
    would let one caller guess at or correlate with another caller's
    requests, a minor but free-to-avoid information leak."""
    return str(uuid.uuid4())


def set_request_id(request_id: str) -> None:
    _request_id.set(request_id)


def get_request_id() -> str | None:
    """`None` outside of any request's context -- e.g. a log line emitted
    at process startup, before any request has come in. Callers (the
    logging filter below) must handle that, not assume a value always
    exists."""
    return _request_id.get()


def reset_request_id() -> None:
    """Back to `None`. Exists for test isolation: pytest runs synchronous
    test functions in one shared context, so a contextvar set in one test
    would otherwise leak into the next one that doesn't set its own --
    see tests/unit/conftest.py's autouse fixture."""
    _request_id.set(None)
