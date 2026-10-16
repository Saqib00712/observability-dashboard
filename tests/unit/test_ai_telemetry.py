"""Phase 5: AI spans/metrics, tested with fakes (no paid API calls)."""

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from prometheus_client.parser import text_string_to_metric_families

from obsdash.ai_telemetry import AITelemetry, OperationKind
from obsdash.metrics import Metrics
from obsdash.tracing import get_tracer
from tests.fakes.ai_fakes import FakeEmbedder, FakeLLM, FakeReranker, FakeRetriever, fake_tool


@pytest.fixture
def env():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    metrics = Metrics()
    return AITelemetry(provider, metrics), exporter, metrics, provider


def _samples(metrics):
    return [s for f in text_string_to_metric_families(metrics.render()[0].decode()) for s in f.samples]


def _val(samples, name, **labels):
    for s in samples:
        if s.name == name and all(s.labels.get(k) == v for k, v in labels.items()):
            return s.value
    return None


def _span(exporter, name):
    return next(s for s in exporter.get_finished_spans() if s.name == name)


def test_model_call_span_attributes_and_tokens(env):
    ai, exporter, metrics, _ = env
    FakeLLM(ai).chat("hi")
    span = _span(exporter, "model_call fake-llm")
    assert span.attributes["gen_ai.operation.name"] == "model_call"
    assert span.attributes["gen_ai.system"] == "fake"
    assert span.attributes["gen_ai.request.model"] == "fake-llm"
    assert span.attributes["gen_ai.usage.input_tokens"] == 12
    assert span.attributes["gen_ai.usage.output_tokens"] == 7
    assert span.status.status_code == StatusCode.OK
    s = _samples(metrics)
    assert _val(s, "obsdash_ai_tokens_total", target="fake-llm", direction="input") == 12
    assert _val(s, "obsdash_ai_tokens_total", target="fake-llm", direction="output") == 7
    assert _val(s, "obsdash_ai_operations_total", operation="model_call", status="ok") == 1


def test_missing_usage_records_no_tokens_not_zero(env):
    ai, exporter, metrics, _ = env
    FakeLLM(ai, usage=None).chat("hi")
    span = _span(exporter, "model_call fake-llm")
    assert "gen_ai.usage.input_tokens" not in span.attributes
    assert _val(_samples(metrics), "obsdash_ai_tokens_total", target="fake-llm") is None


def test_invalid_usage_values_are_ignored(env):
    ai, exporter, metrics, _ = env
    with ai.operation(OperationKind.MODEL_CALL, provider="fake", target="m") as op:
        op.set_usage(input_tokens=-5, output_tokens=True)
        op.set_usage(input_tokens="9")  # type: ignore[arg-type]
    assert _val(_samples(metrics), "obsdash_ai_tokens_total", target="m") is None


def test_failure_marks_error_records_once_and_reraises(env):
    ai, exporter, metrics, _ = env
    with pytest.raises(TimeoutError):
        FakeLLM(ai, fail=TimeoutError("provider timed out")).chat("hi")
    span = _span(exporter, "model_call fake-llm")
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["error.type"] == "TimeoutError"
    assert len([e for e in span.events if e.name == "exception"]) == 1
    s = _samples(metrics)
    assert _val(s, "obsdash_ai_operations_total", operation="model_call", status="error") == 1
    assert _val(s, "obsdash_ai_operation_duration_seconds_count", operation="model_call") == 1


def test_embedding_retrieval_rerank_tool_each_get_their_own_span(env):
    ai, exporter, metrics, _ = env
    FakeEmbedder(ai).embed(["a b c", "d e"])
    docs = FakeRetriever(ai).search("q")
    FakeReranker(ai).rerank(docs)
    fake_tool(ai)
    names = {s.name for s in exporter.get_finished_spans()}
    assert {"embedding fake-embed", "retrieval fake-index", "rerank fake-rerank", "tool_call calculator"} <= names
    assert _span(exporter, "retrieval fake-index").attributes["obsdash.ai.result_count"] == 3
    assert _span(exporter, "embedding fake-embed").attributes["gen_ai.usage.input_tokens"] == 5
    assert "gen_ai.request.model" not in _span(exporter, "retrieval fake-index").attributes
    assert "gen_ai.request.model" not in _span(exporter, "tool_call calculator").attributes


def test_agent_step_nests_model_and_tool_spans(env):
    ai, exporter, _, _ = env
    with ai.operation(OperationKind.AGENT_STEP, provider="local", target="planner"):
        FakeLLM(ai).chat("plan")
        fake_tool(ai)
    agent = _span(exporter, "agent_step planner")
    llm = _span(exporter, "model_call fake-llm")
    tool = _span(exporter, "tool_call calculator")
    assert llm.parent.span_id == agent.context.span_id
    assert tool.parent.span_id == agent.context.span_id
    assert llm.context.trace_id == agent.context.trace_id


def test_ai_span_is_child_of_current_request_span(env):
    ai, exporter, _, provider = env
    with get_tracer(provider).start_as_current_span("GET /ask") as req:
        FakeLLM(ai).chat("hi")
    llm = _span(exporter, "model_call fake-llm")
    assert llm.parent.span_id == req.get_span_context().span_id


def test_no_content_is_recorded_on_spans(env):
    ai, exporter, _, _ = env
    FakeLLM(ai).chat("my secret password is hunter2")
    for sp in exporter.get_finished_spans():
        assert "hunter2" not in str(dict(sp.attributes))


def test_app_exposes_ai_telemetry_handle():
    from obsdash.main import create_app
    app = create_app(TracerProvider())
    assert isinstance(app.state.ai, AITelemetry)
