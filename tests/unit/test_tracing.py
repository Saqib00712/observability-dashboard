"""
Phase 3: spans, parent/child, status, propagation, log correlation.
Uses an isolated TracerProvider + InMemorySpanExporter -- no collector,
no global state.
"""

import json
import re

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from obsdash.config import Settings
from obsdash.main import create_app
from obsdash.metrics import Metrics
from obsdash.middleware import RequestContextMiddleware
from obsdash.otel_setup import build_tracer_provider


def _provider():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _boom_app(provider):
    app = FastAPI()
    app.state.tracer_provider = provider
    app.state.metrics = Metrics()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/boom")
    def boom():
        raise ValueError("secret detail")

    return app


def test_request_creates_span_with_name_and_attributes():
    provider, exporter = _provider()
    TestClient(create_app(provider)).get("/health")
    span = next(s for s in exporter.get_finished_spans() if s.name == "GET /health")
    assert span.attributes["http.method"] == "GET"
    assert span.attributes["http.route"] == "/health"
    assert span.attributes["http.status_code"] == 200
    assert span.status.status_code == StatusCode.OK


def test_child_span_parent_is_request_span():
    provider, exporter = _provider()
    TestClient(create_app(provider)).get("/health")
    spans = {s.name: s for s in exporter.get_finished_spans()}
    parent, child = spans["GET /health"], spans["check_service_identity"]
    assert child.parent.span_id == parent.context.span_id
    assert child.context.trace_id == parent.context.trace_id


def test_exception_marks_span_error_and_records_exception():
    provider, exporter = _provider()
    TestClient(_boom_app(provider), raise_server_exceptions=False).get("/boom")
    span = next(s for s in exporter.get_finished_spans() if s.name == "GET /boom")
    assert span.status.status_code == StatusCode.ERROR
    assert any(e.name == "exception" for e in span.events)


def test_traceparent_header_is_propagated():
    provider, exporter = _provider()
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    tp = f"00-{trace_id}-b7ad6b7169203331-01"
    TestClient(create_app(provider)).get("/health", headers={"traceparent": tp})
    span = next(s for s in exporter.get_finished_spans() if s.name == "GET /health")
    assert format(span.context.trace_id, "032x") == trace_id
    assert format(span.parent.span_id, "016x") == "b7ad6b7169203331"


def test_logs_carry_real_trace_and_span_ids(capsys):
    provider, exporter = _provider()
    TestClient(create_app(provider)).get("/health")
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
    complete = next(l for l in lines if l["message"] == "request_complete")
    assert re.fullmatch(r"[0-9a-f]{32}", complete["trace_id"])
    assert re.fullmatch(r"[0-9a-f]{16}", complete["span_id"])
    span = next(s for s in exporter.get_finished_spans() if s.name == "GET /health")
    assert complete["trace_id"] == format(span.context.trace_id, "032x")


def test_provider_selection_console_vs_otlp():
    console = build_tracer_provider(Settings(otel_exporter_otlp_endpoint=""))
    procs = console._active_span_processor._span_processors
    assert type(procs[0]).__name__ == "SimpleSpanProcessor"
    otlp = build_tracer_provider(Settings(otel_exporter_otlp_endpoint="http://localhost:4318/v1/traces"))
    procs = otlp._active_span_processor._span_processors
    assert type(procs[0]).__name__ == "BatchSpanProcessor"
    otlp.shutdown()
