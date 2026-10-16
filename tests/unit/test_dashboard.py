"""Phase 8: analytics math, dashboard API, empty data, and static page safety."""

import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from obsdash.ai_telemetry import AITelemetry, OperationKind
from obsdash.analytics import bucket_seconds_for, build_errors, build_overview, percentile
from obsdash.main import create_app
from obsdash.pricing import PricingTable
from obsdash.trace_store import TraceStore, TraceStoreExporter

NS = 1_000_000_000
NOW = 1_800_000_000 * NS
BOUNDS = {"spans_in_store": 1, "oldest_ns": NOW, "newest_ns": NOW}


def row(kind="SERVER", name="GET /x", ago_s=10, dur_ms=100, status="OK", attrs=None,
        trace="t" * 32, span="s1", parent=None, msg=None):
    end = NOW - ago_s * NS
    return {"kind": kind, "name": name, "start_ns": end - int(dur_ms * 1e6), "end_ns": end, "status": status,
            "attributes": json.dumps(attrs or {}), "trace_id": trace, "span_id": span, "parent_id": parent,
            "service": "svc", "status_message": msg}


# ---- pure math ----
def test_percentile_nearest_rank():
    v = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert percentile(v, 50) == 50 and percentile(v, 95) == 100 and percentile(v, 10) == 10
    assert percentile([], 95) is None and percentile([7], 99) == 7


def test_bucket_sizes():
    assert bucket_seconds_for(15) == 60 and bucket_seconds_for(60) == 60
    assert bucket_seconds_for(1440) == 1440


def test_overview_requests_errors_latency():
    rows = [row(dur_ms=d, span=f"s{i}", status="ERROR" if i == 0 else "OK")
            for i, d in enumerate([100, 200, 300, 400])]
    o = build_overview(rows, 60, NOW, BOUNDS)
    assert o["requests"]["total"] == 4 and o["requests"]["errors"] == 1 and o["requests"]["error_rate"] == 0.25
    assert o["requests"]["latency_ms"] == {"p50": 200.0, "p95": 400.0, "p99": 400.0}
    assert sum(b["requests"] for b in o["series"]) == 4 and sum(b["errors"] for b in o["series"]) == 1


def test_overview_excludes_rows_outside_window():
    rows = [row(ago_s=10), row(ago_s=2 * 3600, span="old")]
    assert build_overview(rows, 60, NOW, BOUNDS)["requests"]["total"] == 1


def test_overview_empty_has_none_not_zero_latency():
    o = build_overview([], 60, NOW, {"spans_in_store": 0, "oldest_ns": None, "newest_ns": None})
    assert o["requests"]["total"] == 0 and o["requests"]["error_rate"] is None
    assert o["requests"]["latency_ms"]["p95"] is None
    assert o["data_status"]["empty"] is True and o["data_status"]["window_has_data"] is False
    assert all(b["p95_ms"] is None for b in o["series"])  # gaps, not fake zeros


def test_routes_grouped_and_sorted():
    rows = [row(name="GET /a", span="1"), row(name="GET /a", span="2"), row(name="GET /b", span="3", status="ERROR")]
    routes = build_overview(rows, 60, NOW, BOUNDS)["routes"]
    assert [r["route"] for r in routes] == ["GET /a", "GET /b"] and routes[1]["errors"] == 1


def test_ai_totals_count_only_model_and_embedding_calls():
    def ai(op, **a):
        return row("CLIENT", f"{op} x", attrs={"gen_ai.operation.name": op, "gen_ai.system": "p",
                                                "obsdash.ai.target": "m", **a}, span=op)
    rows = [
        ai("model_call", **{"gen_ai.usage.input_tokens": 10, "gen_ai.usage.output_tokens": 5,
                            "obsdash.ai.cost_status": "known", "obsdash.ai.cost_amount": 0.5, "obsdash.ai.cost_currency": "USD"}),
        ai("embedding", **{"gen_ai.usage.input_tokens": 3, "obsdash.ai.cost_status": "unknown_model"}),
        ai("retrieval"), ai("rerank"), ai("agent_step"),
    ]
    o = build_overview(rows, 60, NOW, BOUNDS)
    assert o["ai"]["calls"] == 2 and o["ai"]["input_tokens"] == 13 and o["ai"]["output_tokens"] == 5
    assert o["ai"]["unpriced_calls"] == 1 and o["ai"]["cost_by_currency"] == {"USD": 0.5}
    assert len(o["ai_operations"]) == 5  # every kind still listed per operation


def test_errors_list_only_origin_of_a_propagated_failure():
    t = "a" * 32
    rows = [
        row("SERVER", "GET /ask", status="ERROR", trace=t, span="req", msg="TimeoutError"),
        row("CLIENT", "agent_step", status="ERROR", trace=t, span="agent", parent="req", msg="TimeoutError"),
        row("CLIENT", "model_call x", status="ERROR", trace=t, span="llm", parent="agent",
            attrs={"error.type": "TimeoutError"}),
    ]
    e = build_errors(rows, 60, NOW, 50)
    assert e["total"] == 1 and e["recent"][0]["name"] == "model_call x" and e["recent"][0]["error_type"] == "TimeoutError"


def test_errors_falls_back_to_status_message_for_request_errors():
    e = build_errors([row(status="ERROR", msg="ValueError")], 60, NOW, 50)
    assert e["groups"][0]["error_type"] == "ValueError"


def test_errors_none_when_healthy():
    e = build_errors([row()], 60, NOW, 50)
    assert e["total"] == 0 and e["recent"] == []


# ---- API over the real pipeline ----
@pytest.fixture
def app_client():
    store = TraceStore(":memory:")
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(TraceStoreExporter(store)))
    app = create_app(provider, store)
    app.state.ai = AITelemetry(provider, app.state.metrics, PricingTable())
    yield app, TestClient(app, raise_server_exceptions=False)
    store.close()


def test_api_empty_store_is_graceful(app_client):
    _, c = app_client
    o = c.get("/api/overview").json()
    assert o["data_status"]["empty"] is True and o["requests"]["total"] == 0
    assert c.get("/api/errors").json()["total"] == 0


def test_api_reflects_real_requests_and_failures(app_client):
    app, c = app_client

    @app.get("/boom")
    def boom():
        raise ValueError("x")

    @app.get("/items/{item_id}")
    def item(item_id: int):
        return {}

    for i in range(3):
        c.get(f"/items/{i}")
    c.get("/boom")
    c.get("/nope")  # 404 is not a service error
    o = c.get("/api/overview?window_minutes=15").json()
    assert o["requests"]["total"] == 5 and o["requests"]["errors"] == 1
    routes = {r["route"]: r for r in o["routes"]}
    assert routes["GET /items/{item_id}"]["requests"] == 3  # template, not 3 raw paths
    assert "GET unmatched" in routes and routes["GET unmatched"]["errors"] == 0
    err = c.get("/api/errors").json()
    assert err["total"] == 1 and err["groups"][0]["error_type"] == "ValueError"


def test_dashboard_polling_does_not_pollute_the_store(app_client):
    app, c = app_client
    for path in ("/api/overview", "/api/errors", "/api/traces", "/api/costs/summary", "/metrics", "/dashboard"):
        c.get(path)
    assert app.state.trace_store.span_count() == 0


def test_api_window_validation(app_client):
    _, c = app_client
    assert c.get("/api/overview?window_minutes=0").status_code == 422
    assert c.get("/api/overview?window_minutes=999999").status_code == 422
    assert c.get("/api/errors?limit=0").status_code == 422


def test_span_name_uses_route_template(app_client):
    app, c = app_client

    @app.get("/things/{thing_id}")
    def thing(thing_id: str):
        return {}

    c.get("/things/abc")
    c.get("/things/def")
    names = {t["root_name"] for t in app.state.trace_store.list_traces()}
    assert names == {"GET /things/{thing_id}"}


# ---- the page ----
def test_dashboard_page_is_served(app_client):
    _, c = app_client
    r = c.get("/dashboard")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "obsdash" in r.text
    root = c.get("/", follow_redirects=False)
    assert root.status_code in (302, 307) and root.headers["location"] == "/dashboard"


def test_dashboard_html_has_no_unsafe_dom_or_external_requests():
    html = (Path(__file__).resolve().parents[2] / "obsdash" / "static" / "dashboard.html").read_text(encoding="utf-8")
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in html, f"dashboard.html must not use {banned}"
    assert not re.search(r"(src|href)=[\"']https?://", html)  # works offline, no CDN
    assert "https://" not in html.replace("http://www.w3.org/2000/svg", "")
