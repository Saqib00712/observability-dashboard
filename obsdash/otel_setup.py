"""
Phase 3: OpenTelemetry tracing setup.

Unlike `production-rag`'s optional, fail-open OTel integration -- a
genuine add-on in a project whose job isn't observability -- tracing
here is load-bearing: this whole project exists to answer "what's
happening inside a request," so it's a hard dependency (requirements.txt),
not an extras file. Turning it off would defeat most of this phase's
point.

Exporter choice, explained plainly:
- No `otel_exporter_otlp_endpoint` configured (the default) -> spans
  print as JSON to stdout via `ConsoleSpanExporter`, through a
  `SimpleSpanProcessor` (exports each span immediately, not batched --
  fine for local dev where "see it right away" beats throughput).
  This is real, working tracing with zero infrastructure.
- `otel_exporter_otlp_endpoint` set -> spans export over OTLP/HTTP to
  that endpoint, through a `BatchSpanProcessor` (batches + exports on a
  background thread, so exporting never blocks the request path --
  Phase 8's "don't introduce blocking calls into the request path"
  requirement, honored here from the start rather than retrofitted).
  NOT exercised end-to-end in this environment -- no Collector is
  running here to receive anything -- so this path is tested only as
  far as "the exporter gets constructed with the right endpoint,"
  never as "data actually arrived somewhere real." That distinction
  stays documented, not glossed over.

Why `init_otel()` returns the provider instead of only setting it
globally: tests need an ISOLATED provider (backed by an in-memory
exporter) they can inspect directly, without fighting OpenTelemetry's
own rule that `trace.set_tracer_provider()` only succeeds the first time
per process. `create_app()` accepts an optional `tracer_provider` for
exactly this -- see its docstring in `main.py`.
"""

import logging
from urllib.parse import urlsplit

from opentelemetry import trace
from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor

from obsdash.config import Settings
from obsdash.span_redaction import RedactingSpanExporter
from obsdash.trace_store import TraceStore, TraceStoreExporter

logger = logging.getLogger(__name__)

# Resource limits for background export (Phase 10). Bounded queue: if exporting
# can't keep up, the OLDEST/excess spans are dropped rather than memory growing
# or request threads blocking. 1 s flush keeps the dashboard fresh.
_BATCH_LIMITS = dict(max_queue_size=2048, max_export_batch_size=512, schedule_delay_millis=1000,
                     export_timeout_millis=5000)


def build_tracer_provider(
    settings: Settings, trace_store: TraceStore | None = None, on_export_failure=None
) -> TracerProvider:
    """Build (but do not globally install) a TracerProvider from
    `settings`. Used both by `init_otel()` (the real, global-installing
    path `create_app()` uses by default) and directly by anything that
    wants an isolated provider without touching the process-wide
    singleton."""
    resource = Resource.create({
        SERVICE_NAME: settings.service_name,
        SERVICE_VERSION: "0.1.0",
        DEPLOYMENT_ENVIRONMENT: settings.environment,
    })
    provider = TracerProvider(resource=resource)

    if settings.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint, timeout=5)
        provider.add_span_processor(BatchSpanProcessor(RedactingSpanExporter(exporter), **_BATCH_LIMITS))
        # Log only scheme://host: an endpoint URL can carry credentials.
        parts = urlsplit(settings.otel_exporter_otlp_endpoint)
        logger.info("otel_otlp_exporter_configured", extra={"endpoint_host": f"{parts.scheme}://{parts.hostname}"})
    else:
        provider.add_span_processor(SimpleSpanProcessor(RedactingSpanExporter(ConsoleSpanExporter())))
        logger.info("otel_console_exporter_configured")

    if trace_store is not None:
        # Phase 6: also keep spans locally so the dashboard API can query
        # them. Batched (background thread) so SQLite writes never block
        # a request.
        provider.add_span_processor(
            BatchSpanProcessor(RedactingSpanExporter(TraceStoreExporter(trace_store, on_export_failure)), **_BATCH_LIMITS))

    return provider


def init_otel(
    settings: Settings, trace_store: TraceStore | None = None, on_export_failure=None
) -> TracerProvider:
    """Build a provider AND install it as the process-wide global
    (`trace.set_tracer_provider`). This is what real app startup
    (`create_app()` with no `tracer_provider` override) calls -- exactly
    once per process, since a second call to `set_tracer_provider` is a
    silent no-op by OTel's own design (it logs a warning and keeps the
    first provider). Tests that need their own isolated provider use
    `build_tracer_provider()` directly instead, and never call this."""
    provider = build_tracer_provider(settings, trace_store, on_export_failure)
    trace.set_tracer_provider(provider)
    return provider
