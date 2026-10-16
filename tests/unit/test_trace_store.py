"""Phase 6: trace store, API, bounding, redaction, cross-service propagation."""

import os
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from obsdash.main import create_app
from obsdash.middleware import RequestContextMiddleware
from obsdash.metrics import Metrics
from obsdash.trace_store import TraceStore, TraceStoreExporter, sanitize_attributes
from obsdash.tracing import get_tracer, inject_trace_headers


def _provider(store, service="svc"):
    p = TracerProvider(resource=Resource.create({"service.name": service}))
    p.add_span_processor(SimpleSpanProcessor(TraceStoreExporter(store)))
    return p


@pytest.fixture
def store():
    s = TraceStore(":memory:", max_spans=1000)
    yield s
    s.close()


def test_request_trace_is_stored_and_queryable(store):
    client = TestClient(create_app(_provider(store), store))
    client.get("/health")
    listing = client.get("/api/traces").json()["traces"]
    assert len(listing) == 1
    t = listing[0]
    assert t["root_name"] == "GET /health" and t["span_count"] == 2 and t["has_error"] is False
    assert re.fullmatch(r"[0-9a-f]{32}", t["trace_id"])
    detail = client.get(f"/api/traces/{t['trace_id']}").json()
    root = detail["roots"][0]
    assert root["name"] == "GET /health"
    assert root["children"][0]["name"] == "check_service_identity"
    assert root["service"] == "svc"


def test_self_traffic_is_not_stored(store):
    client = TestClient(create_app(_provider(store), store))
    client.get("/metrics")
    client.get("/api/traces")
    assert store.span_count() == 0


def test_error_trace_filter(store):
    app = create_app(_provider(store), store)

    @app.get("/boom")
    def boom():
        raise ValueError("x")

    client = TestClient(app, raise_server_exceptions=False)
    client.get("/health")
    client.get("/boom")
    all_t = client.get("/api/traces").json()["traces"]
    err_t = client.get("/api/traces?status=error").json()["traces"]
    assert len(all_t) == 2 and len(err_t) == 1
    assert err_t[0]["root_name"] == "GET /boom" and err_t[0]["has_error"] is True
    detail = client.get(f"/api/traces/{err_t[0]['trace_id']}").json()
    assert detail["has_error"] is True
    assert detail["roots"][0]["status"] == "ERROR"


def test_unknown_and_malformed_trace_ids(store):
    client = TestClient(create_app(_provider(store), store))
    assert client.get("/api/traces/" + "0" * 32).status_code == 404
    assert client.get("/api/traces/not-a-trace-id").status_code == 400
    assert client.get("/api/traces/" + "A" * 32).status_code == 400  # uppercase rejected
    assert client.get("/api/traces?status=bogus").status_code == 422
    assert client.get("/api/traces?limit=0").status_code == 422


def test_store_is_bounded_and_drops_oldest():
    store = TraceStore(":memory:", max_spans=5)
    tracer = get_tracer(_provider(store))
    for i in range(12):
        with tracer.start_as_current_span(f"op-{i}"):
            pass
    assert store.span_count() == 5
    names = {t["root_name"] for t in store.list_traces(limit=100)}
    assert names == {f"op-{i}" for i in range(7, 12)}  # newest five survive
    store.close()


def test_secret_attributes_redacted_and_values_truncated(store):
    tracer = get_tracer(_provider(store))
    with tracer.start_as_current_span("x") as sp:
        sp.set_attribute("http.request.header.authorization", "Bearer abc123")
        sp.set_attribute("db.password", "hunter2")
        sp.set_attribute("big", "a" * 5000)
        sp.set_attribute("gen_ai.usage.input_tokens", 10)  # 'token' alone must NOT be redacted
    trace = store.get_trace(store.list_traces()[0]["trace_id"])
    attrs = trace["roots"][0]["attributes"]
    assert attrs["http.request.header.authorization"] == "[redacted]"
    assert attrs["db.password"] == "[redacted]"
    assert len(attrs["big"]) == 256
    assert attrs["gen_ai.usage.input_tokens"] == 10


def test_sanitize_caps_attribute_count():
    assert len(sanitize_attributes({f"k{i}": i for i in range(500)})) == 50


def test_exporter_fails_soft_when_storage_breaks():
    class Broken:
        def add_spans(self, spans):
            raise RuntimeError("disk full")

    from opentelemetry.sdk.trace.export import SpanExportResult
    assert TraceStoreExporter(Broken()).export([]) == SpanExportResult.FAILURE  # no exception escapes


def test_trace_propagates_across_two_services(store):
    """Two separate apps (different service.name, different providers),
    one trace: A calls B with injected headers. Both run in-process here,
    so this proves the propagation mechanics, not real network hops."""
    app_b = create_app(_provider(store, "service-b"), store)
    client_b = TestClient(app_b)

    app_a = create_app(_provider(store, "service-a"), store)

    @app_a.get("/call")
    def call_b():
        return {"b": client_b.get("/health", headers=inject_trace_headers()).status_code}

    client_a = TestClient(app_a)
    assert client_a.get("/call").json() == {"b": 200}

    traces = store.list_traces()
    assert len(traces) == 1  # ONE trace spanning both services
    detail = store.get_trace(traces[0]["trace_id"])
    root = detail["roots"][0]
    assert root["name"] == "GET /call" and root["service"] == "service-a"
    b_span = root["children"][0]
    assert b_span["name"] == "GET /health" and b_span["service"] == "service-b"
    assert b_span["children"][0]["name"] == "check_service_identity"


def test_creating_app_does_not_touch_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    create_app(TracerProvider())
    assert not os.path.exists(tmp_path / "obsdash.db")


def test_real_provider_path_persists_spans_after_shutdown(tmp_path):
    """The production wiring (BatchSpanProcessor + store) -- flushed on shutdown."""
    from obsdash.config import Settings
    from obsdash.otel_setup import build_tracer_provider

    db = str(tmp_path / "t.db")
    store = TraceStore(db)
    provider = build_tracer_provider(Settings(sqlite_path=db), store)
    with get_tracer(provider).start_as_current_span("real-path"):
        pass
    provider.shutdown()  # flushes the batch processor
    assert store.span_count() == 1
    store.close()


def test_old_database_without_status_message_is_migrated(tmp_path):
    """A DB created by Phases 6-7 (no status_message column) must keep working."""
    import sqlite3
    db = str(tmp_path / "old.db")
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE spans (trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_id TEXT,
        name TEXT NOT NULL, kind TEXT, service TEXT, start_ns INTEGER NOT NULL, end_ns INTEGER NOT NULL,
        status TEXT NOT NULL, attributes TEXT NOT NULL, PRIMARY KEY (trace_id, span_id));""")
    con.commit(); con.close()
    store = TraceStore(db)
    with get_tracer(_provider(store)).start_as_current_span("after-migration"):
        pass
    assert store.span_count() == 1
    store.close()


def test_root_name_prefers_the_parentless_span_when_start_times_tie(tmp_path):
    """Found in Phase 11: spans from other systems can share a start
    timestamp with their parent; the listing must still name the real root."""
    from obsdash.trace_store import TraceStore
    store = TraceStore(str(tmp_path / "t.db"))
    import time
    now = time.time_ns()
    row = lambda sid, parent, name: ("a" * 32, sid, parent, name, "INTERNAL", "svc", now, now + 100, "OK", "{}", None)
    store.add_rows([row("2" * 16, "1" * 16, "child"), row("1" * 16, None, "root")])
    assert store.list_traces()[0]["root_name"] == "root"
    store.close()
