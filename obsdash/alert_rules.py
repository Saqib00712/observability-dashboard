"""
Phase 9: alert rule definitions + strict validation.

A rule is "metric COMPARATOR threshold, measured over window_minutes, and
the breach must last for_seconds before it counts". Rules come from a JSON
file (`ALERT_RULES_PATH`, see alert_rules.example.json). An invalid file
stops the app at startup with ALL problems listed -- a silently ignored
rule is an alert that never fires, which is worse than a crash on boot.

Unknown fields are rejected (`extra="forbid"`) so a typo such as
"treshold" is an error, not a rule that quietly uses a default.
"""

import json
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

Metric = Literal[
    "request_count",
    "request_error_rate",
    "request_latency_p95_ms",
    "request_latency_p99_ms",
    "ai_error_rate",
    "ai_unpriced_calls",
    "ai_cost",
    "telemetry_staleness_seconds",
]

# Metrics that are a ratio/percentile over a sample set: they are undefined
# (NO DATA) when there are no samples, and can be guarded with min_samples.
SAMPLED_METRICS = {"request_error_rate", "request_latency_p95_ms", "request_latency_p99_ms", "ai_error_rate"}


class AlertRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")
    name: str = Field(min_length=1, max_length=120)
    metric: Metric
    comparator: Literal[">", ">=", "<", "<="]
    threshold: float
    window_minutes: int = Field(default=5, ge=1, le=1440)
    for_seconds: int = Field(default=0, ge=0, le=86_400)
    min_samples: int = Field(default=0, ge=0, le=1_000_000)
    severity: Literal["info", "warning", "critical"] = "warning"
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    description: str = Field(default="", max_length=500)

    @field_validator("threshold")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("threshold must be a finite number")
        return v

    @model_validator(mode="after")
    def _currency_rules(self) -> "AlertRule":
        if self.metric == "ai_cost" and not self.currency:
            raise ValueError("metric 'ai_cost' requires 'currency' (e.g. \"USD\")")
        if self.metric != "ai_cost" and self.currency:
            raise ValueError("'currency' is only valid with metric 'ai_cost'")
        return self

    def breached(self, value: float) -> bool:
        return {
            ">": value > self.threshold,
            ">=": value >= self.threshold,
            "<": value < self.threshold,
            "<=": value <= self.threshold,
        }[self.comparator]


def parse_rules(data) -> list[AlertRule]:
    entries = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError('alert rules: top level must be {"rules": [...]}')
    rules: list[AlertRule] = []
    problems: list[str] = []
    for i, entry in enumerate(entries):
        try:
            rules.append(AlertRule.model_validate(entry))
        except ValidationError as exc:
            for err in exc.errors():
                where = ".".join(str(p) for p in err["loc"]) or "(rule)"
                problems.append(f"rule[{i}].{where}: {err['msg']}")
    seen: set[str] = set()
    for i, r in enumerate(rules):
        if r.id in seen:
            problems.append(f"duplicate rule id {r.id!r}")
        seen.add(r.id)
    if problems:
        raise ValueError("alert rules invalid:\n  " + "\n  ".join(problems))
    return rules


def load_rules(path: str) -> list[AlertRule]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"alert rules: cannot read {path}: {exc}") from None
    try:
        return parse_rules(json.loads(raw))
    except json.JSONDecodeError as exc:
        raise ValueError(f"alert rules: {path} is not valid JSON: {exc}") from None
