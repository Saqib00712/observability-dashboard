"""
Phase 11: Project 2 (eval-harness) adapter.

Implements eval-harness's REAL extension point -- `RunObserver`
(eval_harness/observability.py), the six hooks `run_evaluation` calls:
on_test_case_start, on_system_error, on_system_output, on_evaluator_error,
on_evaluator_result, on_run_complete. eval-harness is not modified and does
not import obsdash; this module imports eval-harness (so it needs it
installed/importable) and is only loaded when you ask for it.

What it emits (OTel spans, so they reach obsdash via OTLP or in-process):
  eval_run                      one per run (INTERNAL, root)
    eval_test_case              one per test case (INTERNAL)
      system_run                the system under test (CLIENT, operation
                                "agent_step", target = system name)
      eval_evaluator            one per evaluator (INTERNAL)

Honesty notes:
  * The hooks are events, not scopes, so durations are measured between
    hook calls (start of case -> system output; previous event -> evaluator
    result). Evaluator spans are therefore approximate to the hook gaps.
  * A test that FAILS its threshold is a quality result, NOT a telemetry
    error: its span stays OK with eval.passed=false. Only a crash of the
    system or an evaluator marks a span ERROR. So an alert on "AI error
    rate" is not tripped merely because a model scored badly.
  * Token and cost numbers are eval-harness's own `estimated_cost` /
    `token_usage`, recorded as `eval.*` attributes and NOT fed into
    obsdash cost accounting (obsdash would need its own price table, and
    the currency is not stated by eval-harness).
  * Test ids and scores are span attributes, never metric labels.
  * The observer never raises into the runner: a telemetry bug must not
    fail an evaluation run.
"""

import logging
import time

from eval_harness.observability import RunObserver  # ImportError here = eval-harness not installed

from obsdash.tracing import get_tracer, mark_span_error, mark_span_ok
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

logger = logging.getLogger("obsdash.integrations.eval_harness")


def _safe(fn):
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception:  # noqa: BLE001 -- never break the evaluation run
            logger.warning("eval_observer_hook_failed", extra={"hook": fn.__name__}, exc_info=True)
    wrapper.__name__ = fn.__name__
    return wrapper


class ObsdashRunObserver(RunObserver):
    def __init__(self, tracer_provider, system_name: str = "system-under-test", clock=time.time_ns) -> None:
        self._tracer = get_tracer(tracer_provider)
        self._provider = tracer_provider
        self._system = system_name[:100]
        self._clock = clock
        self._root = None
        self._root_ctx = None
        self._case = None
        self._case_ctx = None
        self._case_start = 0
        self._last = 0

    # -- helpers --
    def _ensure_root(self, now: int) -> None:
        if self._root is None:
            self._root = self._tracer.start_span("eval_run", start_time=now, attributes={"eval.system": self._system})
            self._root_ctx = trace.set_span_in_context(self._root)

    def _end_case(self) -> None:
        if self._case is not None:
            if self._case.is_recording() and getattr(self._case, "status", None) is not None \
                    and self._case.status.status_code == StatusCode.UNSET:
                mark_span_ok(self._case)
            self._case.end(end_time=self._last or self._clock())
            self._case = self._case_ctx = None

    # -- hooks --
    @_safe
    def on_test_case_start(self, test_case) -> None:
        now = self._clock()
        self._end_case()
        self._ensure_root(now)
        self._case = self._tracer.start_span(
            "eval_test_case", context=self._root_ctx, start_time=now,
            attributes={"eval.test_id": test_case.test_id})
        self._case_ctx = trace.set_span_in_context(self._case)
        self._case_start = self._last = now

    def _system_span(self, now: int, attrs: dict):
        sp = self._tracer.start_span(
            "system_run", context=self._case_ctx, start_time=self._case_start, kind=trace.SpanKind.CLIENT,
            attributes={"gen_ai.operation.name": "agent_step", "obsdash.ai.target": self._system, **attrs})
        return sp

    @_safe
    def on_system_error(self, test_case, exc: Exception) -> None:
        now = self._clock()
        sp = self._system_span(now, {})
        mark_span_error(sp, exc)
        sp.end(end_time=now)
        self._case.set_status(Status(StatusCode.ERROR))
        self._last = now

    @_safe
    def on_system_output(self, test_case, output) -> None:
        now = self._clock()
        attrs = {}
        if output.latency_ms is not None:
            attrs["eval.system_latency_ms"] = float(output.latency_ms)
        if output.estimated_cost is not None:
            attrs["eval.estimated_cost"] = float(output.estimated_cost)
        if output.token_usage is not None:
            attrs["eval.tokens_total"] = int(output.token_usage.total_tokens)
        sp = self._system_span(now, attrs)
        mark_span_ok(sp)
        sp.end(end_time=now)
        self._last = now

    @_safe
    def on_evaluator_error(self, test_case, evaluator_name: str, exc: Exception) -> None:
        now = self._clock()
        sp = self._tracer.start_span("eval_evaluator", context=self._case_ctx, start_time=self._last,
                                     attributes={"eval.evaluator": evaluator_name[:100]})
        mark_span_error(sp, exc)
        sp.end(end_time=now)
        self._case.set_status(Status(StatusCode.ERROR))
        self._last = now

    @_safe
    def on_evaluator_result(self, test_case, evaluator_name: str, result) -> None:
        now = self._clock()
        attrs = {"eval.evaluator": evaluator_name[:100], "eval.score": float(result.score),
                 "eval.passed": bool(result.passed)}
        if result.estimated_cost is not None:
            attrs["eval.estimated_cost"] = float(result.estimated_cost)
        sp = self._tracer.start_span("eval_evaluator", context=self._case_ctx, start_time=self._last, attributes=attrs)
        mark_span_ok(sp)
        sp.end(end_time=now)
        self._last = now

    @_safe
    def on_run_complete(self, run) -> None:
        now = self._clock()
        self._end_case()
        self._ensure_root(now)
        self._root.set_attributes({
            "eval.run_id": run.run_id, "eval.total_tests": run.total_tests,
            "eval.passed_tests": run.passed_tests, "eval.failed_tests": run.failed_tests,
            "eval.pass_rate": float(run.pass_rate), "eval.estimated_cost": float(run.estimated_cost),
        })
        mark_span_ok(self._root)
        self._root.end(end_time=now)
        self._root = None
        flush = getattr(self._provider, "force_flush", None)
        if flush:
            flush()

    def close(self) -> None:
        """Call if a run is abandoned mid-way, so the open spans are ended."""
        self._end_case()
        if self._root is not None:
            self._root.end(end_time=self._clock())
            self._root = None
