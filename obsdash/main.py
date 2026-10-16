"""
Phase 1-9: the FastAPI application.

`create_app(tracer_provider=None)`: a factory so tests can inject an
isolated OTel TracerProvider (OTel's global provider can only be set once
per process). In real runs (`obsdash.main:app`) no provider is passed and
`init_otel(settings)` builds the real one.

`lifespan` runs once at start/stop; on stop it flushes and shuts the
provider down so buffered spans (BatchSpanProcessor) aren't lost.
"""

import asyncio
import contextlib
from contextlib import asynccontextmanager
from typing import AsyncIterator

import hashlib
import base64
import ipaddress
import logging
import re

from fastapi import Depends, FastAPI
from pathlib import Path

from fastapi.responses import JSONResponse, RedirectResponse, Response
from opentelemetry.sdk.trace import TracerProvider

from obsdash.ai_telemetry import AITelemetry
from obsdash.alert_engine import AlertEvaluator, AlertStateStore, WebhookNotifier
from obsdash.alert_rules import load_rules
from obsdash.auth import require_token
from obsdash.api_alerts import router as alerts_router
from obsdash.api_costs import router as costs_router
from obsdash.api_dashboard import router as dashboard_router
from obsdash.api_traces import router as traces_router
from obsdash.config import get_settings
from obsdash.logging_setup import configure_logging
from obsdash.metrics import Metrics
from obsdash.middleware import RequestContextMiddleware
from obsdash.otel_setup import init_otel
from obsdash.otlp_ingest import router as otlp_router
from obsdash.pricing import PricingTable
from obsdash.trace_store import QueryTimeout, TraceStore
from obsdash.tracing import child_span, get_tracer


_DASHBOARD_HTML = Path(__file__).parent / "static" / "dashboard.html"
logger = logging.getLogger("obsdash.app")


def dashboard_csp(html: str) -> str:
    """Strict Content-Security-Policy for the dashboard page: no
    'unsafe-inline', no external hosts. The single inline <script> and
    <style> are allowed by their SHA-256 hash, so any injected script
    (the classic XSS payoff) would simply not run."""
    def h(tag: str) -> str:
        m = re.search(rf"<{tag}>(.*?)</{tag}>", html, re.S)
        return "'sha256-" + base64.b64encode(hashlib.sha256(m.group(1).encode()).digest()).decode() + "'" if m else "'none'"
    return ("default-src 'none'; script-src " + h("script") + "; style-src " + h("style") +
            "; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    alerts = getattr(app.state, "alerts", None)
    task = None
    if alerts is not None and alerts.rules:
        interval = get_settings().alert_eval_interval_seconds
        task = asyncio.create_task(alerts.run_forever(interval))
    yield
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    provider = getattr(app.state, "tracer_provider", None)
    if provider is not None and hasattr(provider, "shutdown"):
        provider.shutdown()  # flushes buffered spans into the store first
    for name in ("trace_store", "alert_store"):
        closable = getattr(app.state, name, None)
        if closable is not None:
            closable.close()


def create_app(
    tracer_provider: TracerProvider | None = None,
    trace_store: TraceStore | None = None,
    alert_store: AlertStateStore | None = None,
) -> FastAPI:
    settings = get_settings()
    # Eager (not only in lifespan): TestClient without a `with` block
    # doesn't run lifespan, and logs should still be structured.
    configure_logging()

    app = FastAPI(
        title="obsdash",
        description="Real-time observability dashboard for AI systems (Project 4).",
        version="0.1.0",
        lifespan=lifespan,
    )
    # Phase 6: a caller-supplied provider (tests) owns its own exporters;
    # otherwise the real provider is built with the store attached.
    app.state.metrics = Metrics()
    app.state.api_token = settings.api_token.get_secret_value()
    app.state.trace_store = trace_store or TraceStore(
        settings.sqlite_path, settings.trace_max_spans, settings.trace_retention_hours, settings.query_timeout_seconds)
    app.state.tracer_provider = tracer_provider or init_otel(
        settings, app.state.trace_store, app.state.metrics.trace_export_failures_total.inc)
    if not app.state.api_token and not _is_loopback(settings.host):
        logger.warning("api_unauthenticated_on_non_loopback_host", extra={"host": settings.host})
    # Phase 5: the handle other code (RAG/agent integrations, Phase 11) uses.
    # Fail fast at startup on a bad pricing file rather than mis-price later.
    pricing = PricingTable.from_file(settings.pricing_table_path) if settings.pricing_table_path else PricingTable()
    app.state.pricing = pricing
    app.state.ai = AITelemetry(app.state.tracer_provider, app.state.metrics, pricing)
    app.add_middleware(RequestContextMiddleware)

    # Phase 9: rules load (and validate) at startup; a bad file stops the app.
    rules = load_rules(settings.alert_rules_path) if settings.alert_rules_path else []
    app.state.alert_store = alert_store or AlertStateStore(settings.sqlite_path, settings.alert_max_events, settings.query_timeout_seconds)
    webhook = settings.alert_webhook_url.get_secret_value()
    notifier = WebhookNotifier(webhook, app.state.metrics) if webhook else None
    app.state.alerts = AlertEvaluator(rules, app.state.trace_store, app.state.alert_store, app.state.metrics, notifier)
    app.include_router(alerts_router)
    app.include_router(traces_router)
    app.include_router(costs_router)
    app.include_router(dashboard_router)
    app.state.otlp_max_body_bytes = settings.otlp_max_body_bytes
    app.state.otlp_max_spans_per_request = settings.otlp_max_spans_per_request
    app.state.otlp_enrich_known = settings.otlp_enrich_known_services
    app.include_router(otlp_router)

    @app.get("/health")
    def health() -> dict:
        tracer = get_tracer(app.state.tracer_provider)
        with child_span(tracer, "check_service_identity", {"service.name": settings.service_name}):
            return {
                "status": "ok",
                "service": settings.service_name,
                "environment": settings.environment,
                "version": "0.1.0",
            }

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        # Liveness probe for Docker/orchestrators. Its spans are not stored (see trace_store
        # self-traffic list), so polling it never counts as traffic or keeps alerts quiet.
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/dashboard")

    dashboard_html = _DASHBOARD_HTML.read_text(encoding="utf-8")
    dashboard_headers = {"Content-Security-Policy": dashboard_csp(dashboard_html), "Cache-Control": "no-store"}

    @app.get("/dashboard", include_in_schema=False)
    def dashboard() -> Response:
        # The page itself is a static shell with no data; the API calls it
        # makes are what the token protects.
        return Response(dashboard_html, media_type="text/html; charset=utf-8", headers=dashboard_headers)

    @app.exception_handler(QueryTimeout)
    def query_timeout(request, exc) -> JSONResponse:
        return JSONResponse(status_code=503, content={"error": "query_timeout"}, headers={"Retry-After": "5"})

    @app.get("/metrics", include_in_schema=False, dependencies=[Depends(require_token)])
    def metrics_endpoint() -> Response:
        body, content_type = app.state.metrics.render()
        return Response(content=body, media_type=content_type)

    return app


app = create_app()
