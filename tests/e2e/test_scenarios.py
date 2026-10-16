"""
Phase 12: end-to-end scenarios. A synthetic service (routes added to a real
obsdash app) runs success / slow / failure workloads through the REAL
pipeline: middleware -> spans -> SQLite store -> analytics -> Prometheus ->
logs -> alert engine. Each scenario then checks that every observable
surface agrees with the others and with the exact numbers we generated.
No paid AI calls: fakes only. Only the alert CLOCK is simulated.
"""

import io
import json
import logging
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from obsdash.alert_engine import AlertEvaluator, AlertStateStore
from obsdash.alert_rules import parse_rules
from obsdash.ai_telemetry import OperationKind
from obsdash.config import get_settings
from obsdash.logging_setup import CorrelationFilter, JsonFormatter
from obsdash.main import create_app
from obsdash.trace_store import TraceStore, TraceStoreExporter
from tests.fakes.ai_fakes import FakeEmbedder, FakeLLM, FakeReranker, FakeRetriever

NS = 1_000_000_000
PRICING = str(Path(__file__).resolve().parents[2] / "pricing.example.json")

RULES = {"rules": [
    {"id": "err", "name": "Error rate", "metric": "request_error_rate", "comparator": ">", "threshold": 0.2,
     "window_minutes": 5, "for_seconds": 60, "min_samples": 5, "severity": "critical"},
    {"id": "slow", "name": "p95 slow", "metric": "request_latency_p95_ms", "comparator": ">", "threshold": 200,
     "window_minutes": 5, "for_seconds": 0, "min_samples": 5, "severity": "warning"},
    {"id": "ai-err", "name": "AI errors", "metric": "ai_error_rate", "comparator": ">", "threshold": 0.1,
     "window_minutes": 5, "for_seconds": 0, "min_samples": 5, "severity": "warning"},
    {"id": "unpriced", "name": "Unpriced", "metric": "ai_unpriced_calls", "comparator": ">", "threshold": 0,
     "window_minutes": 5, "for_seconds": 0, "severity": "info"},
]}


class World:
    def __init__(self, tmp_path, monkeypatch, db_name="w.db"):
        monkeypatch.setenv("PRICING_TABLE_PATH", PRICING)
        monkeypatch.setenv("SQLITE_PATH", str(tmp_path / db_name))
        get_settings.cache_clear()
        self.db = str(tmp_path / db_name)
        self.store = TraceStore(self.db)
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(TraceStoreExporter(self.store)))
        self.app = create_app(provider, self.store, AlertStateStore(":memory:"))
        ai = self.app.state.ai
        app = self.app

        @app.get("/work")
        def work():
            FakeRetriever(ai).search("q")
            FakeReranker(ai).rerank(["a", "b", "c"])
            FakeEmbedder(ai).embed(["alpha beta gamma"])  # 3 input tokens
            return {"reply": FakeLLM(ai, usage=(12, 7)).chat("hi")}

        @app.get("/slow")
        def slow():
            time.sleep(0.25)
            FakeLLM(ai).chat("hi")
            return {}

        @app.get("/fail")
        def fail():
            FakeLLM(ai, fail=RuntimeError("upstream down")).chat("hi")

        @app.get("/mystery")
        def mystery():
            with ai.operation(OperationKind.MODEL_CALL, provider="acme", target="mystery-model") as op:
                op.set_usage(input_tokens=10, output_tokens=5)
            return {}

        # Capture the app's real JSON log lines (same formatter + correlation filter) in memory.
        self.log_stream = io.StringIO()
        self._handler = logging.StreamHandler(self.log_stream)
        self._handler.setFormatter(JsonFormatter())
        self._handler.addFilter(CorrelationFilter())
        logging.getLogger().addHandler(self._handler)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.t0 = time.time_ns()
        self.offset = 0
        self.ev = AlertEvaluator(parse_rules(RULES), self.store, AlertStateStore(":memory:"), app.state.metrics,
                                 None, clock=lambda: self.t0 + int(self.offset * NS))

    def hit(self, path, n):
        return [self.client.get(path).status_code for _ in range(n)]

    def at(self, offset):
        self.offset = offset
        self.ev.evaluate_once()
        return {r["id"]: r["state"] for r in self.ev.snapshot()["rules"]}

    def metric(self, name, **labels):
        """Value of one Prometheus sample from the real /metrics text."""
        for line in self.client.get("/metrics").text.splitlines():
            if line.startswith(name + "{") or line.startswith(name + " "):
                got = dict(re.findall(r'(\w+)="([^"]*)"', line))
                if all(got.get(k) == v for k, v in labels.items()):
                    return float(line.rsplit(" ", 1)[1])
        return None

    def overview(self):
        return self.client.get("/api/overview?window_minutes=60").json()

    def logs(self):
        return [json.loads(l) for l in self.log_stream.getvalue().splitlines() if l.startswith("{")]

    def close(self):
        logging.getLogger().removeHandler(self._handler)
        self.store.close()
        get_settings.cache_clear()


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    yield w
    w.close()


# ---------------------------------------------------------------- success
def test_success_scenario_all_surfaces_agree(world):
    assert set(world.hit("/work", 20)) == {200}
    logs = world.logs()

    # Prometheus
    assert world.metric("obsdash_http_requests_total", route="/work", status_class="2xx") == 20
    assert world.metric("obsdash_ai_operations_total", operation="model_call", target="fake-llm", status="ok") == 20
    assert world.metric("obsdash_ai_tokens_total", target="fake-llm", direction="input") == 240
    assert world.metric("obsdash_ai_tokens_total", target="fake-llm", direction="output") == 140
    assert world.metric("obsdash_ai_cost_total", target="fake-llm", currency="USD") == pytest.approx(0.00052)

    # Dashboard API
    ov = world.overview()
    assert ov["requests"]["total"] == 20 and ov["requests"]["errors"] == 0
    assert ov["ai"]["calls"] == 40                       # 20 model calls + 20 embeddings (billed kinds)
    assert ov["ai"]["input_tokens"] == 240 + 60 and ov["ai"]["output_tokens"] == 140
    assert ov["ai"]["unpriced_calls"] == 0
    assert ov["ai"]["cost_by_currency"]["USD"] == pytest.approx(0.000526, abs=1e-9)
    ops = {o["operation"]: o for o in ov["ai_operations"]}
    assert ops["retrieval"]["calls"] == 20 and ops["rerank"]["calls"] == 20
    assert ops["retrieval"]["cost_by_currency"] == {}     # non-billed kinds never get a cost
    assert world.client.get("/api/costs/summary").json()["totals"]["cost_by_currency"]["USD"] == pytest.approx(0.000526, abs=1e-9)

    # Logs <-> traces: every request log line points at a real stored trace
    done = [l for l in logs if l["message"] == "request_complete" and l["operation"] == "GET /work"]
    assert len(done) == 20
    traces = {t["trace_id"]: t for t in world.client.get("/api/traces?limit=100").json()["traces"]}
    assert {l["trace_id"] for l in done} <= set(traces)
    tid = done[0]["trace_id"]
    detail = world.client.get(f"/api/traces/{tid}").json()
    root = detail["roots"][0]
    assert root["name"] == "GET /work" and detail["span_count"] == 5
    assert sorted(c["name"] for c in root["children"]) == sorted(
        [c["name"] for c in root["children"]])  # children are direct descendants of the request span
    assert len(root["children"]) == 4 and not detail["has_error"]

    # All four sources report the same request count
    server_spans = len(world.store.query_spans(kinds=("SERVER",)))
    assert server_spans == ov["requests"]["total"] == len(done) == 20


# ------------------------------------------------------------------- slow
def test_slow_scenario_latency_and_alert(world):
    world.hit("/work", 10)
    world.hit("/slow", 10)
    ov = world.overview()
    routes = {r["route"]: r for r in ov["routes"]}
    assert routes["GET /slow"]["p95_ms"] >= 250          # sleep(0.25) is a hard lower bound
    assert routes["GET /work"]["p95_ms"] < routes["GET /slow"]["p95_ms"]
    assert ov["requests"]["latency_ms"]["p95"] >= 250 and ov["requests"]["errors"] == 0
    hist = world.metric("obsdash_http_request_duration_seconds_count", route="/slow")
    assert hist == 10
    assert world.at(0)["slow"] == "FIRING"               # for_seconds=0 -> immediate
    assert world.at(1)["err"] == "OK"                    # slowness is not an error


# ---------------------------------------------------------------- failure
def test_failure_scenario_counts_errors_and_alert_lifecycle(world):
    world.hit("/work", 15)
    codes = world.hit("/fail", 10)
    assert set(codes) == {500}
    logs = world.logs()

    assert world.metric("obsdash_http_requests_total", route="/fail", status_class="5xx") == 10
    assert world.metric("obsdash_ai_operations_total", operation="model_call", target="fake-llm", status="error") == 10
    ov = world.overview()
    assert ov["requests"]["total"] == 25 and ov["requests"]["errors"] == 10
    assert ov["requests"]["error_rate"] == pytest.approx(0.4)
    # Failed calls without token usage are skipped for cost: no fabricated spend, no phantom unpriced
    assert world.metric("obsdash_ai_cost_total", target="fake-llm", currency="USD") == pytest.approx(15 * 26e-6)  # 12 in x $1/M + 7 out x $2/M per successful call
    # The two pages must agree on "unpriced" (found from a real dashboard screenshot in Phase 14):
    # failed calls with no usage are neither priced nor unpriced.
    cs = world.client.get("/api/costs/summary").json()
    llm = next(m for m in cs["models"] if m["model"] == "fake-llm")
    assert llm["calls"] == 25 and llm["priced_calls"] == 15 and llm["unpriced_calls"] == 0
    assert cs["totals"]["unpriced_calls"] == ov["ai"]["unpriced_calls"] == 0
    err = world.client.get("/api/errors?window_minutes=60").json()
    assert err["total"] == 10                            # one ORIGIN per failed request, not one per ancestor span
    assert err["groups"][0]["count"] == 10 and err["groups"][0]["kind"] == "CLIENT"

    # Logs: 10 request_error lines, each correlated to a stored ERROR trace
    errs = [l for l in logs if l["message"] == "request_error"]
    assert len(errs) == 10
    stored = {t["trace_id"] for t in world.client.get("/api/traces?status=error&limit=100").json()["traces"]}
    assert {l["trace_id"] for l in errs} <= stored and len(stored) == 10

    # Alert lifecycle on the real data
    assert world.at(0)["err"] == "PENDING"
    assert world.at(30)["err"] == "PENDING"
    assert world.at(61)["err"] == "FIRING"
    assert world.metric("obsdash_alert_firing", rule="err") == 1
    assert world.at(62)["ai-err"] == "FIRING"            # 10/(15+15+10) AI error share ... > 10%
    world.hit("/work", 120)                              # recovery traffic: 10/145 = 6.9%
    assert world.at(90)["err"] == "OK"
    assert world.metric("obsdash_alert_firing", rule="err") == 0
    transitions = [(e["rule_id"], e["from_state"], e["to_state"]) for e in reversed(world.ev.snapshot()["events"])
                   if e["rule_id"] == "err"]
    assert transitions == [("err", None, "PENDING"), ("err", "PENDING", "FIRING"), ("err", "FIRING", "OK")]


# --------------------------------------------------------- unknown pricing
def test_unknown_model_is_unpriced_not_free(world):
    world.hit("/mystery", 6)
    ov = world.overview()
    assert ov["ai"]["calls"] == 6 and ov["ai"]["unpriced_calls"] == 6
    assert ov["ai"]["cost_by_currency"] == {}
    assert ov["ai"]["input_tokens"] == 60                # tokens are still real and counted
    assert world.metric("obsdash_ai_unpriced_operations_total", target="mystery-model", reason="unknown_model") == 6
    assert world.metric("obsdash_ai_cost_total", target="mystery-model") is None
    assert world.at(0)["unpriced"] == "FIRING"


# ------------------------------------------------------------- persistence
def test_data_survives_restart(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.hit("/work", 5)
    before = w.overview()["requests"]["total"]
    w.close()
    reopened = TraceStore(str(tmp_path / "w.db"))
    assert reopened.span_count() >= 5 * 5
    from obsdash.analytics import build_overview
    now = time.time_ns()
    rows = reopened.query_spans(since_ns=now - 3600 * NS)
    assert build_overview(rows, 60, now, reopened.bounds())["requests"]["total"] == before == 5
    reopened.close()


# ---------------------------------------------------- silence & empty states
def test_empty_system_is_reported_honestly(world):
    ov = world.overview()
    assert ov["data_status"]["empty"] is True and ov["requests"]["total"] == 0
    assert ov["requests"]["error_rate"] is None and ov["requests"]["latency_ms"]["p95"] is None  # unknown, not 0
    states = world.at(0)
    assert states["err"] == "NO_DATA" and states["slow"] == "NO_DATA"


def test_dashboard_page_served_with_strict_csp(world):
    r = world.client.get("/dashboard")
    assert r.status_code == 200 and "unsafe-inline" not in r.headers["content-security-policy"]
    assert "default-src 'none'" in r.headers["content-security-policy"]


def test_liveness_probe_is_not_counted_as_traffic(world):
    """Found on the owner's Docker run: the container healthcheck polled /health every 15 s and showed up
    as real requests, which would keep 'no traffic' / 'stale telemetry' alerts from ever firing."""
    assert set(world.hit("/healthz", 30)) == {200}
    ov = world.overview()
    assert ov["requests"]["total"] == 0 and ov["data_status"]["empty"] is True
    assert world.at(0)["err"] == "NO_DATA"
    world.hit("/work", 1)
    assert world.overview()["requests"]["total"] == 1
