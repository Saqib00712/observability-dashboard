"""
Phase 5: AI-specific telemetry -- spans + metrics for model calls,
embeddings, retrieval, reranking, tool calls and agent steps.

One context manager does it all:

    with ai.operation(OperationKind.MODEL_CALL, provider="openai", target="gpt-4o-mini") as op:
        reply = client.chat(...)
        op.set_usage(input_tokens=reply.usage.prompt, output_tokens=reply.usage.completion)

It (1) opens a child span under the current request span, (2) times the
operation, (3) records outcome (ok/error) on span and metrics, and (4)
counts tokens -- but ONLY when the caller supplies them. A provider that
reports no usage yields NO token series and NO span usage attributes; we
never write a made-up 0 or an estimate (a zero would look like "free").

Privacy: the recorder exposes only usage/result-count setters. There is
deliberately no way to attach prompt text, completion text, documents or
tool arguments. Those can contain user secrets; telemetry never needs them.

Span attributes follow OpenTelemetry's GenAI naming where it exists
(gen_ai.operation.name, gen_ai.system, gen_ai.request.model,
gen_ai.usage.input_tokens / output_tokens) so standard tooling
recognises them.

Metric labels (all low-cardinality): operation, provider, target, status.
`target` = model name / tool name / index name / agent name, chosen by
the caller from a small known set. Callers must never pass user input.
"""

import time
from contextlib import contextmanager
from enum import Enum
from typing import Iterator

from opentelemetry.trace import Span, SpanKind

from obsdash.metrics import Metrics
from obsdash.redaction import OTHER, LabelLimiter, sanitize_label
from obsdash.pricing import CostStatus, PricingTable
from obsdash.tracing import get_tracer, mark_span_error, mark_span_ok


MAX_DISTINCT_TARGETS = 100  # distinct (provider, target) pairs before folding into __other__


class OperationKind(str, Enum):
    MODEL_CALL = "model_call"
    EMBEDDING = "embedding"
    RETRIEVAL = "retrieval"
    RERANK = "rerank"
    TOOL_CALL = "tool_call"
    AGENT_STEP = "agent_step"


_MODEL_KINDS = {OperationKind.MODEL_CALL, OperationKind.EMBEDDING, OperationKind.RERANK}
# Kinds that bill by tokens and therefore get cost accounting (Phase 7).
_COSTED_KINDS = {OperationKind.MODEL_CALL, OperationKind.EMBEDDING}


class AIOperation:
    """What the caller gets inside the `with` block. Intentionally tiny."""

    def __init__(self, span: Span, provider: str, target: str, metrics: Metrics) -> None:
        self._span = span
        self._provider = provider
        self._target = target
        self._metrics = metrics
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None

    def set_usage(self, input_tokens: int | None = None, output_tokens: int | None = None) -> None:
        """Record token usage the provider actually reported. None or a
        negative number is ignored (not recorded as 0). Calling it again
        ADDS to the totals (e.g. a streamed response reporting in pieces);
        the span shows the running total and the counter gets the delta."""
        for direction, value, attr in (
            ("input", input_tokens, "gen_ai.usage.input_tokens"),
            ("output", output_tokens, "gen_ai.usage.output_tokens"),
        ):
            if value is None or isinstance(value, bool) or not isinstance(value, int) or value < 0:
                continue
            total = (getattr(self, f"{direction}_tokens") or 0) + value
            setattr(self, f"{direction}_tokens", total)
            self._span.set_attribute(attr, total)
            self._metrics.ai_tokens_total.labels(self._provider, self._target, direction).inc(value)

    def set_result_count(self, count: int) -> None:
        """E.g. documents retrieved / reranked. A count, never the content."""
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            self._span.set_attribute("obsdash.ai.result_count", count)


class AITelemetry:
    def __init__(self, tracer_provider, metrics: Metrics, pricing: PricingTable | None = None) -> None:
        self._tracer = get_tracer(tracer_provider)
        self._metrics = metrics
        self._pricing = pricing or PricingTable()
        # Phase 10: enforce the cardinality contract in code, not just in docs.
        self._limiter = LabelLimiter(MAX_DISTINCT_TARGETS)

    @contextmanager
    def operation(self, kind: OperationKind, *, provider: str, target: str) -> Iterator[AIOperation]:
        provider, target = sanitize_label(provider), sanitize_label(target)
        if self._limiter.admit((provider, target)) is None:
            provider = target = OTHER
            self._metrics.label_overflow_total.labels("ai_target").inc()
        attrs = {
            "gen_ai.operation.name": kind.value,
            "gen_ai.system": provider,
            "obsdash.ai.target": target,
        }
        if kind in _MODEL_KINDS:
            attrs["gen_ai.request.model"] = target

        status = "ok"
        start = time.perf_counter()
        # record_exception/set_status_on_exception off: we do it ourselves
        # (mark_span_error) so the exception is recorded exactly once.
        with self._tracer.start_as_current_span(
            f"{kind.value} {target}",
            kind=SpanKind.CLIENT,
            attributes=attrs,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            op = AIOperation(span, provider, target, self._metrics)
            try:
                yield op
                mark_span_ok(span)
            except BaseException as exc:
                status = "error"
                span.set_attribute("error.type", type(exc).__name__)
                mark_span_error(span, exc)
                raise
            finally:
                if kind in _COSTED_KINDS:
                    self._account_cost(kind, span, op, provider, target, status)
                elapsed = time.perf_counter() - start
                self._metrics.ai_operations_total.labels(kind.value, provider, target, status).inc()
                self._metrics.ai_operation_duration.labels(kind.value, provider, target).observe(elapsed)

    def _account_cost(self, kind, span: Span, op: AIOperation, provider: str, target: str, status: str) -> None:
        """Cost accounting for one finished model/embedding operation.
        A FAILED call that reported no tokens is skipped: it isn't
        "unpriced", it just didn't complete."""
        if status == "error" and op.input_tokens is None and op.output_tokens is None:
            return
        result = self._pricing.compute(provider, target, op.input_tokens, op.output_tokens)
        span.set_attribute("obsdash.ai.cost_status", result.status.value)
        if result.status is CostStatus.KNOWN:
            amount = float(round(result.amount, 8))
            span.set_attribute("obsdash.ai.cost_amount", amount)
            span.set_attribute("obsdash.ai.cost_currency", result.currency)
            self._metrics.ai_cost_total.labels(provider, target, result.currency).inc(amount)
        else:
            self._metrics.ai_unpriced_total.labels(provider, target, result.status.value).inc()
