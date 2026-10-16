"""Phase 11: the Project 2 observer, driven by eval-harness's REAL
`run_evaluation` with its own MockSystem (no paid AI calls)."""

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

pytest.importorskip("eval_harness", reason="eval-harness repo not found next to this one")
from tests.integration import conftest  # noqa: E402,F401  (puts eval-harness on sys.path)

from eval_harness.dataset import Dataset  # noqa: E402
from eval_harness.evaluators import ExactMatchEvaluator  # noqa: E402
from eval_harness.mock_system import MockSystem  # noqa: E402
from eval_harness.models import TestCase  # noqa: E402
from eval_harness.runner import run_evaluation  # noqa: E402
from eval_harness.system_output import SystemOutput, TokenUsage  # noqa: E402

from obsdash.integrations.eval_harness_observer import ObsdashRunObserver  # noqa: E402

CASES = [
    TestCase(test_id="ok", input="q1", expected_output="Paris"),
    TestCase(test_id="wrong", input="q2", expected_output="Paris"),
    TestCase(test_id="crash", input="q3", expected_output="Paris"),
]


def _run(observer, evaluators=None, system=None):
    system = system or MockSystem({
        "ok": SystemOutput(output="Paris", latency_ms=12.0, estimated_cost=0.001, token_usage=TokenUsage(input_tokens=5, output_tokens=2)),
        "wrong": SystemOutput(output="Lyon", latency_ms=8.0),
    }, name="mock")
    ds = Dataset(dataset_id="d", name="d", version="1", test_cases=CASES)
    return asyncio.run(run_evaluation(ds, system, evaluators or [ExactMatchEvaluator()], observer=observer))


@pytest.fixture
def prov():
    exp = InMemorySpanExporter()
    p = TracerProvider()
    p.add_span_processor(SimpleSpanProcessor(exp))
    return p, exp


def test_run_produces_one_trace_with_expected_shape(prov):
    p, exp = prov
    run = _run(ObsdashRunObserver(p, system_name="mock"))
    spans = exp.get_finished_spans()
    by = lambda n: [s for s in spans if s.name == n]
    assert len(by("eval_run")) == 1 and len(by("eval_test_case")) == 3
    assert len(by("system_run")) == 3 and len(by("eval_evaluator")) == 2  # crash case: system failed, no evaluators ran
    assert len({s.context.trace_id for s in spans}) == 1
    root = by("eval_run")[0]
    assert root.parent is None and root.attributes["eval.run_id"] == run.run_id
    assert root.attributes["eval.total_tests"] == 3 and root.attributes["eval.passed_tests"] == 1
    for c in by("eval_test_case"):
        assert c.parent.span_id == root.context.span_id
    for s in by("system_run") + by("eval_evaluator"):
        assert s.parent.span_id in {c.context.span_id for c in by("eval_test_case")}
    assert all(s.start_time <= s.end_time for s in spans)


def test_failed_threshold_is_not_an_error_but_a_crash_is(prov):
    p, exp = prov
    _run(ObsdashRunObserver(p, system_name="mock"))
    spans = exp.get_finished_spans()
    evals = {s.attributes["eval.score"]: s for s in spans if s.name == "eval_evaluator"}
    assert {e.attributes["eval.passed"] for e in evals.values()} == {True, False}
    assert all(e.status.status_code == StatusCode.OK for e in evals.values())  # bad score != telemetry error
    cases = {s.attributes["eval.test_id"]: s for s in spans if s.name == "eval_test_case"}
    assert cases["crash"].status.status_code == StatusCode.ERROR
    assert cases["wrong"].status.status_code == StatusCode.OK
    sysruns = [s for s in spans if s.name == "system_run"]
    assert sum(s.status.status_code == StatusCode.ERROR for s in sysruns) == 1
    err = next(s for s in sysruns if s.status.status_code == StatusCode.ERROR)
    assert any(e.name == "exception" for e in err.events)
    assert not any("Traceback" in str(e.attributes) for e in err.events)  # type + redacted message only


def test_system_span_is_an_obsdash_ai_operation_and_carries_eval_numbers(prov):
    p, exp = prov
    _run(ObsdashRunObserver(p, system_name="mock"))
    ok = next(s for s in exp.get_finished_spans() if s.name == "system_run" and s.attributes.get("eval.tokens_total"))
    assert ok.kind.name == "CLIENT"
    assert ok.attributes["gen_ai.operation.name"] == "agent_step" and ok.attributes["obsdash.ai.target"] == "mock"
    assert ok.attributes["eval.tokens_total"] == 7 and ok.attributes["eval.estimated_cost"] == 0.001
    assert "obsdash.ai.cost_amount" not in ok.attributes  # eval-harness's estimate is not re-labelled as obsdash cost


def test_observer_never_breaks_the_run(prov):
    p, _ = prov

    class Boom(ObsdashRunObserver):
        def _ensure_root(self, now):
            raise RuntimeError("telemetry bug")

    run = _run(Boom(p, system_name="mock"))
    assert run.total_tests == 3 and run.passed_tests == 1  # evaluation result unaffected


def test_results_are_identical_with_and_without_observer(prov):
    p, _ = prov
    a, b = _run(ObsdashRunObserver(p, system_name="mock")), _run(None)
    strip = lambda r: [(x.test_id, x.evaluator, x.score, x.passed) for x in r.results]
    assert strip(a) == strip(b) and a.pass_rate == b.pass_rate


def test_close_ends_open_spans_for_an_abandoned_run(prov):
    p, exp = prov
    obs = ObsdashRunObserver(p, system_name="mock")
    obs.on_test_case_start(CASES[0])
    obs.close()
    assert {s.name for s in exp.get_finished_spans()} == {"eval_test_case", "eval_run"}


def test_eval_run_is_visible_in_obsdash_via_otlp(live_server):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    import httpx

    p = TracerProvider(resource=Resource.create({"service.name": "eval-harness"}))
    p.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=live_server["url"] + "/v1/traces")))
    _run(ObsdashRunObserver(p, system_name="mock"))
    p.shutdown()
    t = httpx.get(live_server["url"] + "/api/traces").json()["traces"]
    run = next(x for x in t if x["service"] == "eval-harness")
    assert run["root_name"] == "eval_run" and run["span_count"] == 1 + 3 + 3 + 2
    ov = httpx.get(live_server["url"] + "/api/overview?window_minutes=60").json()
    assert any(o["operation"] == "agent_step" and o["target"] == "mock" and o["calls"] == 3 and o["errors"] == 1
               for o in ov["ai_operations"])
