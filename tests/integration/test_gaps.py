"""Phase 12: paths coverage measurement showed untested; each asserts real behaviour."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as pb
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from obsdash.config import get_settings
from obsdash.main import create_app, _is_loopback
from obsdash.span_redaction import RedactingSpanExporter
from obsdash.trace_store import TraceStore
from tests.integration.test_otlp_ingest import NOW, PB, _request


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "g.db"))
    get_settings.cache_clear()
    store = TraceStore(str(tmp_path / "g.db"))
    yield TestClient(create_app(trace_store=store)), store
    store.close()
    get_settings.cache_clear()


def test_non_scalar_attributes_dropped_and_attribute_count_capped(client):
    c, store = client
    req = _request(spans=[dict(name="many", attrs={f"k{i}": i for i in range(200)})])
    sp = req.resource_spans[0].scope_spans[0].spans[0]
    arr = sp.attributes.add()
    arr.key = "an_array"
    arr.value.array_value.values.add().string_value = "x"
    assert c.post("/v1/traces", content=req.SerializeToString(), headers=PB).status_code == 200
    (row,) = store.query_spans(kinds=("INTERNAL",))
    attrs = json.loads(row["attributes"])
    assert "an_array" not in attrs and len(attrs) <= 64


def test_chunked_upload_without_content_length_is_still_bounded(client):
    c, store = client
    c.app.state.otlp_max_body_bytes = 2048

    def gen():
        for _ in range(10):
            yield b"a" * 1000

    r = c.post("/v1/traces", content=gen(), headers=PB)  # generator body => chunked, no Content-Length
    assert r.status_code == 413 and store.span_count() == 0


def test_redacting_exporter_forwards_flush():
    inner = InMemorySpanExporter()
    assert RedactingSpanExporter(inner).force_flush() is True


def test_loopback_detection():
    assert _is_loopback("localhost") and _is_loopback("127.0.0.1") and _is_loopback("::1")
    assert not _is_loopback("0.0.0.0") and not _is_loopback("example.com")


def test_evaluator_crash_marks_spans_error_but_run_continues():
    pytest.importorskip("eval_harness")
    from tests.integration import conftest  # noqa: F401
    from eval_harness.dataset import Dataset
    from eval_harness.mock_system import MockSystem
    from eval_harness.models import TestCase
    from eval_harness.runner import run_evaluation
    from eval_harness.system_output import SystemOutput
    from obsdash.integrations.eval_harness_observer import ObsdashRunObserver

    class Crashy:
        name = "crashy"

        async def evaluate(self, test_case, output):
            raise RuntimeError("judge exploded")

    exp = InMemorySpanExporter()
    p = TracerProvider()
    p.add_span_processor(SimpleSpanProcessor(exp))
    ds = Dataset(dataset_id="d", name="d", version="1", test_cases=[TestCase(test_id="a", input="q")])
    run = asyncio.run(run_evaluation(ds, MockSystem({"a": SystemOutput(output="x")}), [Crashy()],
                                     observer=ObsdashRunObserver(p, system_name="m")))
    assert run.failed_tests == 1  # the harness still completed and recorded the failure
    spans = {s.name: s for s in exp.get_finished_spans()}
    assert spans["eval_evaluator"].status.status_code == StatusCode.ERROR
    assert spans["eval_test_case"].status.status_code == StatusCode.ERROR
    assert spans["system_run"].status.status_code == StatusCode.OK
