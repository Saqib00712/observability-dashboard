"""Phase 11: end-to-end over a REAL socket with REAL exporters.

1. The stock OpenTelemetry OTLP exporter (the same class production-rag
   uses) -> live obsdash.
2. production-rag's own, unmodified `init_otel` + `span()` in a separate
   process -> live obsdash. Skipped if production-rag is not found.
"""

import json
import subprocess
import sys
import textwrap
import time

import httpx
import pytest
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from tests.integration.conftest import PRODUCTION_RAG


def test_stock_otlp_exporter_reaches_obsdash(live_server):
    provider = TracerProvider(resource=Resource.create({"service.name": "demo-app"}))
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=live_server["url"] + "/v1/traces")))
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("parent"):
        with tracer.start_as_current_span("child"):
            pass
    provider.shutdown()
    traces = httpx.get(live_server["url"] + "/api/traces").json()
    t = next(x for x in traces["traces"] if x["service"] == "demo-app")
    assert t["span_count"] == 2 and t["root_name"] == "parent"
    detail = httpx.get(f"{live_server['url']}/api/traces/{t['trace_id']}").json()
    assert detail["roots"][0]["name"] == "parent" and detail["roots"][0]["children"][0]["name"] == "child"


@pytest.mark.skipif(PRODUCTION_RAG is None, reason="production-rag repo not found next to this one")
def test_production_rag_unmodified_appears_in_obsdash(live_server):
    child = textwrap.dedent(f"""
        import time
        from app.config import Settings
        from app.observability.otel_export import init_otel
        from app.observability.tracing import span, start_trace
        init_otel(Settings(otel_enabled=True, otel_exporter_otlp_endpoint="{live_server['url']}/v1/traces"))
        start_trace("t1")
        with span("retrieval", document_id="d1"):
            with span("rerank", candidates=4):
                pass
        with span("generation", sources=2):
            time.sleep(0.02)
        try:
            with span("embedding", chunks=3):
                raise ValueError("boom with sk-{'a1B2c3D4' * 5}")
        except ValueError:
            pass
    """)
    proc = subprocess.run([sys.executable, "-c", child], cwd=PRODUCTION_RAG, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-2000:]
    deadline = time.time() + 10
    while live_server["store"].span_count() < 4 and time.time() < deadline:
        time.sleep(0.1)  # the child's BatchSpanProcessor flushes at exit
    rows = {r["name"]: r for r in live_server["store"].query_spans(kinds=("CLIENT", "INTERNAL"))}
    assert {"retrieval", "rerank", "generation", "embedding"} <= set(rows)
    assert all(r["service"] == "production-rag" for r in rows.values())
    assert rows["embedding"]["status"] == "ERROR"
    assert "sk-a1B2" not in json.dumps({k: dict(v) for k, v in rows.items()})  # secret in the error message is redacted
    ov = httpx.get(live_server["url"] + "/api/overview?window_minutes=60").json()
    ops = {o["operation"]: o for o in ov["ai_operations"]}
    assert ops["model_call"]["calls"] == 1 and ops["embedding"]["errors"] == 1
    assert ops["model_call"]["unpriced_calls"] == 1  # no tokens were sent -> cost stays UNKNOWN, not zero
    assert ov["ai"]["calls"] == 2 and ov["ai"]["unpriced_calls"] == 2  # model_call + embedding, both unknown cost
    assert ov["ai"]["cost_by_currency"] == {}  # never a fabricated zero
