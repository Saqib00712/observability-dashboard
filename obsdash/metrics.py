"""
Phase 4: Prometheus metrics for obsdash's own HTTP traffic.

Metric types (the only three you need so far):
- Counter: only goes up (requests served). Prometheus computes rates from it.
- Gauge: goes up and down (requests currently in flight).
- Histogram: counts observations into buckets (request duration), so
  percentiles (p95) can be computed later with histogram_quantile().

Cardinality rule: every distinct label-value combination becomes its own
time series stored forever. So labels are ONLY low-cardinality values:
method, route TEMPLATE (e.g. "/items/{id}", never "/items/123"), and
status class. request_id / trace_id / user input are NEVER labels --
they belong in logs and traces. Unknown routes (404 scanners hitting
random URLs) collapse into the single label "unmatched".

Each `Metrics` owns its own CollectorRegistry (not prometheus_client's
global default), so tests and multiple apps in one process never collide
with "Duplicated timeseries" errors.
"""

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest

# Seconds. Tuned for an HTTP API: 5ms .. 10s.
DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

# AI calls are slower than HTTP handlers: 10ms .. 60s.
AI_DURATION_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)

UNMATCHED_ROUTE = "unmatched"


KNOWN_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


def normalize_method(method: str) -> str:
    """Clients can send ANY string as the HTTP method; as a label that is a
    cardinality attack. Anything unusual becomes "OTHER"."""
    return method if method in KNOWN_METHODS else "OTHER"


def status_class(status_code: int) -> str:
    return f"{status_code // 100}xx"


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests_total = Counter(
            "obsdash_http_requests_total",
            "HTTP requests handled.",
            ["method", "route", "status_class"],
            registry=self.registry,
        )
        self.request_duration = Histogram(
            "obsdash_http_request_duration_seconds",
            "HTTP request duration in seconds.",
            ["method", "route"],
            buckets=DURATION_BUCKETS,
            registry=self.registry,
        )
        # Phase 5: AI operations. Labels are low-cardinality by contract
        # (see ai_telemetry.py): operation kind, provider, target, status.
        self.ai_operations_total = Counter(
            "obsdash_ai_operations_total",
            "AI operations (model calls, embeddings, retrieval, rerank, tools, agent steps).",
            ["operation", "provider", "target", "status"],
            registry=self.registry,
        )
        self.ai_operation_duration = Histogram(
            "obsdash_ai_operation_duration_seconds",
            "AI operation duration in seconds.",
            ["operation", "provider", "target"],
            buckets=AI_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.ai_tokens_total = Counter(
            "obsdash_ai_tokens_total",
            "Tokens reported by providers (only when reported).",
            ["provider", "target", "direction"],
            registry=self.registry,
        )
        self.ai_cost_total = Counter(
            "obsdash_ai_cost_total",
            "Cost of AI operations whose price was known (currency in label).",
            ["provider", "target", "currency"],
            registry=self.registry,
        )
        self.ai_unpriced_total = Counter(
            "obsdash_ai_unpriced_operations_total",
            "Model operations whose cost could NOT be computed (unknown_model / no_usage).",
            ["provider", "target", "reason"],
            registry=self.registry,
        )
        # Phase 9: alerting. `rule` label is bounded by the rules file.
        self.alert_firing = Gauge(
            "obsdash_alert_firing", "1 while an alert rule is FIRING.", ["rule", "severity"], registry=self.registry)
        self.alert_evaluation_failures_total = Counter(
            "obsdash_alert_evaluation_failures_total", "Alert rule evaluations that raised.", registry=self.registry)
        self.alert_notification_failures_total = Counter(
            "obsdash_alert_notification_failures_total", "Webhook notifications that failed.", registry=self.registry)
        self.alert_last_evaluation_timestamp = Gauge(
            "obsdash_alert_last_evaluation_timestamp_seconds",
            "Unix time of the last completed alert evaluation pass.", registry=self.registry)
        # Phase 10: self-monitoring of the safeguards.
        self.trace_export_failures_total = Counter(
            "obsdash_trace_export_failures_total", "Span batches the local trace store failed to save.",
            registry=self.registry)
        self.label_overflow_total = Counter(
            "obsdash_label_overflow_total", "Label values folded into __other__ by a cardinality cap.",
            ["source"], registry=self.registry)
        # Phase 11: OTLP receiver (bounded label sets only).
        self.otlp_requests_total = Counter(
            "obsdash_otlp_requests_total", "OTLP/HTTP trace export requests received, by outcome.",
            ["outcome"], registry=self.registry)
        self.otlp_spans_total = Counter(
            "obsdash_otlp_spans_total", "Spans received over OTLP, by result.",
            ["result"], registry=self.registry)
        self.in_flight = Gauge(
            "obsdash_http_requests_in_flight",
            "HTTP requests currently being handled.",
            registry=self.registry,
        )

    def observe_request(self, method: str, route: str, status_code: int, duration_s: float) -> None:
        method = normalize_method(method)
        self.requests_total.labels(method, route, status_class(status_code)).inc()
        self.request_duration.labels(method, route).observe(duration_s)

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
