"""Phase 7: cost wired into AI telemetry, metrics, and the summary API (fakes only)."""

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client.parser import text_string_to_metric_families

from obsdash.ai_telemetry import AITelemetry, OperationKind
from obsdash.main import create_app
from obsdash.metrics import Metrics
from obsdash.pricing import PricingTable
from obsdash.trace_store import TraceStore, TraceStoreExporter
from tests.fakes.ai_fakes import FakeEmbedder, FakeLLM

PRICES = PricingTable.from_dict({"models": [
    {"provider": "fake", "model": "fake-llm", "input_per_million": "1.00", "output_per_million": "2.00"},
    {"provider": "fake", "model": "fake-embed", "input_per_million": "0.10", "output_per_million": None},
]})


@pytest.fixture
def env():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    metrics = Metrics()
    return AITelemetry(provider, metrics, PRICES), exporter, metrics


def _samples(metrics):
    return [s for f in text_string_to_metric_families(metrics.render()[0].decode()) for s in f.samples]


def _val(samples, name, **labels):
    for s in samples:
        if s.name == name and all(s.labels.get(k) == v for k, v in labels.items()):
            return s.value
    return None


def _attrs(exporter, name):
    return dict(next(s for s in exporter.get_finished_spans() if s.name == name).attributes)


def test_known_model_cost_on_span_and_metric(env):
    ai, exporter, metrics = env
    FakeLLM(ai, usage=(1_000_000, 500_000)).chat("x")
    a = _attrs(exporter, "model_call fake-llm")
    assert a["obsdash.ai.cost_status"] == "known"
    assert a["obsdash.ai.cost_amount"] == 2.0 and a["obsdash.ai.cost_currency"] == "USD"
    assert _val(_samples(metrics), "obsdash_ai_cost_total", target="fake-llm", currency="USD") == 2.0


def test_unknown_model_counted_as_unpriced_with_no_cost(env):
    ai, exporter, metrics = env
    with ai.operation(OperationKind.MODEL_CALL, provider="fake", target="mystery") as op:
        op.set_usage(input_tokens=10, output_tokens=10)
    a = _attrs(exporter, "model_call mystery")
    assert a["obsdash.ai.cost_status"] == "unknown_model" and "obsdash.ai.cost_amount" not in a
    s = _samples(metrics)
    assert _val(s, "obsdash_ai_unpriced_operations_total", target="mystery", reason="unknown_model") == 1
    assert _val(s, "obsdash_ai_cost_total", target="mystery") is None


def test_missing_usage_is_no_usage_not_zero_cost(env):
    ai, exporter, metrics = env
    FakeLLM(ai, usage=None).chat("x")
    assert _attrs(exporter, "model_call fake-llm")["obsdash.ai.cost_status"] == "no_usage"
    s = _samples(metrics)
    assert _val(s, "obsdash_ai_unpriced_operations_total", target="fake-llm", reason="no_usage") == 1
    assert _val(s, "obsdash_ai_cost_total", target="fake-llm") is None


def test_embedding_costed_from_input_tokens_only(env):
    ai, exporter, _ = env
    FakeEmbedder(ai).embed(["a b c d e"])  # 5 tokens
    a = _attrs(exporter, "embedding fake-embed")
    assert a["obsdash.ai.cost_status"] == "known"
    assert a["obsdash.ai.cost_amount"] == pytest.approx(5 * 0.10 / 1_000_000)


def test_failed_call_without_usage_is_not_counted_as_unpriced(env):
    ai, exporter, metrics = env
    with pytest.raises(TimeoutError):
        FakeLLM(ai, fail=TimeoutError("t")).chat("x")
    assert "obsdash.ai.cost_status" not in _attrs(exporter, "model_call fake-llm")
    assert _val(_samples(metrics), "obsdash_ai_unpriced_operations_total", target="fake-llm") is None


def test_failed_call_that_reported_usage_is_still_costed(env):
    ai, exporter, _ = env
    with pytest.raises(RuntimeError):
        with ai.operation(OperationKind.MODEL_CALL, provider="fake", target="fake-llm") as op:
            op.set_usage(input_tokens=1_000_000, output_tokens=0)
            raise RuntimeError("cut off mid-stream")
    assert _attrs(exporter, "model_call fake-llm")["obsdash.ai.cost_amount"] == 1.0


def test_repeated_set_usage_accumulates(env):
    ai, exporter, metrics = env
    with ai.operation(OperationKind.MODEL_CALL, provider="fake", target="fake-llm") as op:
        op.set_usage(input_tokens=500_000, output_tokens=100_000)
        op.set_usage(output_tokens=400_000)
    a = _attrs(exporter, "model_call fake-llm")
    assert a["gen_ai.usage.output_tokens"] == 500_000
    assert _val(_samples(metrics), "obsdash_ai_tokens_total", direction="output", target="fake-llm") == 500_000
    assert a["obsdash.ai.cost_amount"] == pytest.approx(0.5 + 1.0)


def test_retrieval_and_tool_get_no_cost_fields(env):
    ai, exporter, _ = env
    with ai.operation(OperationKind.RETRIEVAL, provider="fake", target="idx"):
        pass
    assert "obsdash.ai.cost_status" not in _attrs(exporter, "retrieval idx")


def _api_env():
    store = TraceStore(":memory:")
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(TraceStoreExporter(store)))
    app = create_app(provider, store)
    app.state.ai = AITelemetry(provider, app.state.metrics, PRICES)
    return app, TestClient(app)


def test_cost_summary_api_aggregates_and_reports_unpriced():
    app, client = _api_env()
    ai = app.state.ai
    FakeLLM(ai, usage=(1_000_000, 500_000)).chat("a")
    FakeLLM(ai, usage=(1_000_000, 500_000)).chat("b")
    FakeLLM(ai, usage=None).chat("c")  # unpriced: no usage
    with ai.operation(OperationKind.MODEL_CALL, provider="fake", target="mystery") as op:
        op.set_usage(input_tokens=7, output_tokens=7)
    body = client.get("/api/costs/summary").json()
    m = {x["model"]: x for x in body["models"]}
    assert m["fake-llm"]["calls"] == 3 and m["fake-llm"]["priced_calls"] == 2
    assert m["fake-llm"]["unpriced_calls"] == 1
    assert m["fake-llm"]["input_tokens"] == 2_000_000
    assert m["fake-llm"]["cost_by_currency"] == {"USD": 4.0}
    assert m["mystery"]["unpriced_calls"] == 1 and m["mystery"]["cost_by_currency"] == {}
    assert body["totals"] == {"calls": 4, "unpriced_calls": 2, "cost_by_currency": {"USD": 4.0}}


def test_cost_summary_empty_and_validation():
    _, client = _api_env()
    assert client.get("/api/costs/summary").json()["totals"]["calls"] == 0
    assert client.get("/api/costs/summary?window_minutes=0").status_code == 422
    assert client.get("/api/costs/summary?window_minutes=60").status_code == 200


def test_summary_window_excludes_old_spans():
    app, client = _api_env()
    FakeLLM(app.state.ai, usage=(1, 1)).chat("x")
    store = app.state.trace_store
    store._db().execute("UPDATE spans SET start_ns = start_ns - ?", (3 * 3600 * 10**9,))
    assert client.get("/api/costs/summary?window_minutes=60").json()["totals"]["calls"] == 0
    assert client.get("/api/costs/summary?window_minutes=600").json()["totals"]["calls"] == 1


def test_bad_pricing_file_fails_app_startup(tmp_path, monkeypatch):
    bad = tmp_path / "p.json"
    bad.write_text('{"models":[{"provider":"p","model":"m","input_per_million":"-5"}]}')
    monkeypatch.setenv("PRICING_TABLE_PATH", str(bad))
    from obsdash.config import get_settings
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match=">= 0"):
            create_app(TracerProvider())
    finally:
        monkeypatch.delenv("PRICING_TABLE_PATH")
        get_settings.cache_clear()
