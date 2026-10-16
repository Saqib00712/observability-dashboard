"""
Phase 2 + 3 + 4: `RequestContextMiddleware` -- the one place a request ID AND
an OTel span get assigned, logged/recorded, and safely error-handled for
every request this service receives. (Phase 2 built the request-id half;
Phase 3 adds the span half on top, in the same middleware rather than a
second one, so "when did logging's request_id get set" and "when did the
span start" can never drift out of sync with each other.)
"""

import logging
import re
import time

from opentelemetry.trace import Status as SpanStatus
from opentelemetry.trace import StatusCode
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from obsdash.correlation import new_request_id, set_request_id
from obsdash.metrics import UNMATCHED_ROUTE, normalize_method
from obsdash.tracing import get_tracer, mark_span_error, mark_span_ok, request_span

logger = logging.getLogger("obsdash.request")

_REQUEST_ID_HEADER = "X-Request-ID"
# A caller-supplied request id is only honoured if it is short and plain.
# Anything else (log-injection attempts, megabytes of junk) gets replaced.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def _harden(headers, path: str) -> None:
    for k, v in _SECURITY_HEADERS.items():
        headers.setdefault(k, v)
    if path.startswith(("/api/", "/metrics")):
        headers.setdefault("Cache-Control", "no-store")  # telemetry must not sit in caches


def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", None) or UNMATCHED_ROUTE


class RequestContextMiddleware(BaseHTTPMiddleware):
    @staticmethod
    def _label_span_with_route(span, request: Request) -> None:
        """Once the router has matched, rename the span from the RAW path
        ("GET /items/123") to the route TEMPLATE ("GET /items/{id}"), so
        trace names and dashboard route tables stay bounded -- same rule
        as the Prometheus labels (Phase 4)."""
        label = _route_label(request)
        span.update_name(f"{normalize_method(request.method)} {label}")
        span.set_attribute("http.route", label)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(_REQUEST_ID_HEADER)
        request_id = incoming if incoming and _VALID_REQUEST_ID.match(incoming) else new_request_id()
        set_request_id(request_id)

        tracer = get_tracer(request.app.state.tracer_provider)
        metrics = request.app.state.metrics
        method = normalize_method(request.method)
        span_name = f"{method} {request.url.path[:200]}"

        # Phase 4: metrics are recorded in `finally`, so a request that
        # crashes is still counted (as 5xx) and in_flight never leaks.
        status_code = 500
        metrics.in_flight.inc()
        start = time.perf_counter()
        logger.info("request_start", extra={"operation": span_name})
        try:
            with request_span(
                tracer,
                span_name,
                headers=dict(request.headers),
                attributes={
                    "http.method": method,
                    "http.route": request.url.path,
                    "obsdash.request_id": request_id,
                },
            ) as span:
                try:
                    response = await call_next(request)
                except Exception as exc:
                    self._label_span_with_route(span, request)
                    duration_ms = round((time.perf_counter() - start) * 1000, 2)
                    mark_span_error(span, exc)
                    # Full detail server-side only; client gets a generic
                    # message plus the request_id.
                    logger.error(
                        "request_error",
                        extra={"operation": span_name, "duration_ms": duration_ms},
                        exc_info=exc,
                    )
                    error_response = JSONResponse(
                        status_code=500,
                        content={"error": "internal_server_error", "request_id": request_id},
                        headers={_REQUEST_ID_HEADER: request_id},
                    )
                    _harden(error_response.headers, request.url.path)
                    return error_response

                self._label_span_with_route(span, request)
                status_code = response.status_code
                span.set_attribute("http.status_code", status_code)
                if status_code < 500:
                    mark_span_ok(span)
                else:
                    span.set_status(SpanStatus(StatusCode.ERROR))

                # Logged INSIDE the span so the line carries its ids.
                duration_ms = round((time.perf_counter() - start) * 1000, 2)
                logger.info(
                    "request_complete",
                    extra={"operation": span_name, "status_code": status_code, "duration_ms": duration_ms},
                )
            response.headers[_REQUEST_ID_HEADER] = request_id
            _harden(response.headers, request.url.path)
            return response
        finally:
            # The router stores the matched route in scope; use its TEMPLATE
            # ("/items/{id}"), never the raw path, to keep cardinality bounded.
            route_label = _route_label(request)
            metrics.in_flight.dec()
            metrics.observe_request(
                request.method, route_label, status_code, time.perf_counter() - start
            )
