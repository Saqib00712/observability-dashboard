"""Phase 9: alert rules, state machine, persistence, notification, failure handling.
Real pipeline (requests -> spans -> store -> analytics) with an injectable clock."""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from prometheus_client.parser import text_string_to_metric_families

from obsdash import alert_engine
from obsdash.alert_engine import AlertEvaluator, AlertStateStore, WebhookNotifier
from obsdash.alert_rules import AlertRule, load_rules, parse_rules
from obsdash.main import create_app
from obsdash.metrics import Metrics
from obsdash.trace_store import TraceStore, TraceStoreExporter

NS = 1_000_000_000


# ---------------- rule validation ----------------
def rule_dict(**over):
    d = {"id": "r1", "name": "R1", "metric": "request_error_rate", "comparator": ">", "threshold": 0.1}
    d.update(over)
    return d


def test_valid_rule_defaults():
    r = parse_rules({"rules": [rule_dict()]})[0]
    assert r.window_minutes == 5 and r.for_seconds == 0 and r.severity == "warning" and r.breached(0.2)


@pytest.mark.parametrize("comparator,value,expected", [(">", 5, False), (">=", 5, True), ("<", 5, False), ("<=", 5, True)])
def test_comparators_at_the_boundary(comparator, value, expected):
    r = AlertRule(**rule_dict(comparator=comparator, threshold=5))
    assert r.breached(value) is expected


@pytest.mark.parametrize("bad", [
    rule_dict(metric="cpu_temperature"),
    rule_dict(comparator="=="),
    rule_dict(treshold=1),                      # typo -> unknown field
    rule_dict(threshold=float("nan")),
    rule_dict(threshold=float("inf")),
    rule_dict(window_minutes=0),
    rule_dict(for_seconds=-1),
    rule_dict(severity="apocalyptic"),
    rule_dict(id="Has Spaces"),
    rule_dict(metric="ai_cost"),                # needs currency
    rule_dict(currency="USD"),                  # currency only for ai_cost
])
def test_invalid_rules_rejected(bad):
    with pytest.raises(ValueError):
        parse_rules({"rules": [bad]})


def test_all_problems_reported_together_and_duplicates_rejected():
    with pytest.raises(ValueError) as e:
        parse_rules({"rules": [rule_dict(metric="nope"), rule_dict(id="a"), rule_dict(id="a")]})
    msg = str(e.value)
    assert "rule[0].metric" in msg and "duplicate rule id 'a'" in msg


def test_bad_top_level_and_files(tmp_path):
    with pytest.raises(ValueError, match="top level"):
        parse_rules({"nope": []})
    with pytest.raises(ValueError, match="cannot read"):
        load_rules(str(tmp_path / "missing.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{oops")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_rules(str(bad))


def test_example_rules_file_loads():
    assert len(load_rules("alert_rules.example.json")) == 6


# ---------------- harness ----------------
class RecordingNotifier:
    def __init__(self):
        self.sent = []

    def notify(self, payload):
        self.sent.append(payload)
        return True


class Env:
    def __init__(self, rules, alert_store=None, notifier=None):
        self.store = TraceStore(":memory:")
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(TraceStoreExporter(self.store)))
        self.app = create_app(provider, self.store, AlertStateStore(":memory:"))

        @self.app.get("/ok")
        def ok():
            return {}

        @self.app.get("/boom")
        def boom():
            raise ValueError("x")

        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.metrics = self.app.state.metrics
        self.alert_store = alert_store or AlertStateStore(":memory:")
        self.notifier = notifier if notifier is not None else RecordingNotifier()
        self.t0 = time.time_ns()
        self.offset_s = 0
        self.ev = AlertEvaluator(parse_rules({"rules": rules}), self.store, self.alert_store, self.metrics,
                                 self.notifier, clock=lambda: self.t0 + int(self.offset_s * NS))

    def traffic(self, ok=0, fail=0):
        for _ in range(ok):
            self.client.get("/ok")
        for _ in range(fail):
            self.client.get("/boom")

    def at(self, offset_s):
        self.offset_s = offset_s
        self.ev.evaluate_once()
        return {r["id"]: r for r in self.ev.snapshot()["rules"]}

    def events(self):
        return [(e["from_state"], e["to_state"]) for e in reversed(self.ev.snapshot()["events"])]


ERR = dict(id="err", name="Err", metric="request_error_rate", comparator=">", threshold=0.2,
           window_minutes=5, for_seconds=60, min_samples=5, severity="critical")


# ---------------- state machine ----------------
def test_fires_after_for_seconds_and_notifies_once():
    env = Env([ERR])
    env.traffic(ok=2, fail=8)                      # 80% errors
    assert env.at(0)["err"]["state"] == "PENDING"
    assert env.at(30)["err"]["state"] == "PENDING"
    assert env.notifier.sent == []                 # nothing before for_seconds elapses
    assert env.at(61)["err"]["state"] == "FIRING"
    assert env.at(90)["err"]["state"] == "FIRING"  # stays firing
    assert [p["status"] for p in env.notifier.sent] == ["firing"]   # exactly once
    p = env.notifier.sent[0]
    assert p["rule"] == "err" and p["severity"] == "critical" and p["value"] == pytest.approx(0.8)
    assert env.events() == [("PENDING", "FIRING")] or env.events() == [(None, "PENDING"), ("PENDING", "FIRING")]


def test_for_seconds_zero_fires_immediately():
    env = Env([{**ERR, "for_seconds": 0}])
    env.traffic(fail=10)
    assert env.at(0)["err"]["state"] == "FIRING" and len(env.notifier.sent) == 1


def test_transient_spike_never_fires_or_notifies():
    env = Env([ERR])
    env.traffic(ok=1, fail=9)
    assert env.at(0)["err"]["state"] == "PENDING"
    env.traffic(ok=190)                            # recovers before 60 s
    assert env.at(30)["err"]["state"] == "OK"
    assert env.at(200)["err"]["state"] == "OK"
    assert env.notifier.sent == []
    assert ("PENDING", "FIRING") not in env.events()


def test_pending_timer_resets_after_a_gap_in_the_breach():
    env = Env([ERR])
    env.traffic(ok=1, fail=9)
    env.at(0)                                      # PENDING since 0
    env.traffic(ok=190)
    env.at(40)                                     # OK (breach ended)
    env.traffic(fail=400)                          # breach again
    assert env.at(80)["err"]["state"] == "PENDING"  # NOT firing: timer restarted at 80
    assert env.at(141)["err"]["state"] == "FIRING"


def test_resolves_and_notifies_resolved():
    env = Env([{**ERR, "for_seconds": 0}])
    env.traffic(ok=2, fail=8)
    assert env.at(0)["err"]["state"] == "FIRING"
    env.traffic(ok=190)                            # 8/200 = 4% < 20%
    assert env.at(10)["err"]["state"] == "OK"
    assert [p["status"] for p in env.notifier.sent] == ["firing", "resolved"]
    assert env.events()[-1] == ("FIRING", "OK")
    assert env.ev.snapshot()["firing"] == 0


def test_no_data_is_not_ok_and_not_firing():
    env = Env([ERR])
    snap = env.at(0)["err"]
    assert snap["state"] == "NO_DATA" and snap["no_data"] is True and "no_samples" in snap["message"]
    assert env.notifier.sent == [] and env.events() == []   # silence is not an event


def test_insufficient_samples_is_no_data_not_a_false_alarm():
    env = Env([ERR])
    env.traffic(fail=2)                            # 100% errors but only 2 requests (< min_samples 5)
    snap = env.at(0)["err"]
    assert snap["state"] == "NO_DATA" and "insufficient_samples" in snap["message"]


def test_firing_alert_stays_firing_when_telemetry_disappears_then_resolves_on_recovery():
    env = Env([{**ERR, "for_seconds": 0}])
    env.traffic(ok=2, fail=8)
    assert env.at(0)["err"]["state"] == "FIRING"
    snap = env.at(600)["err"]                      # all spans now outside the 5-min window
    assert snap["state"] == "FIRING" and snap["no_data"] is True and "still FIRING" in snap["message"]
    assert [p["status"] for p in env.notifier.sent] == ["firing"]   # no false "resolved"
    env.offset_s = 600
    env.t0 = time.time_ns() - 600 * NS + 0         # fresh healthy traffic "now" relative to clock
    env.traffic(ok=30)
    assert env.at(600)["err"]["state"] == "OK"
    assert [p["status"] for p in env.notifier.sent] == ["firing", "resolved"]


def test_staleness_rule_fires_when_telemetry_stops_and_is_no_data_when_never_seen():
    stale = dict(id="stale", name="Stale", metric="telemetry_staleness_seconds", comparator=">", threshold=300)
    empty = Env([stale])
    assert empty.at(0)["stale"]["state"] == "NO_DATA"       # never received anything
    env = Env([stale])
    env.traffic(ok=3)
    assert env.at(10)["stale"]["state"] == "OK"
    snap = env.at(400)["stale"]
    assert snap["state"] == "FIRING" and snap["last_value"] > 300


def test_traffic_stopped_rule_uses_zero_as_a_real_value():
    rule = dict(id="quiet", name="Quiet", metric="request_count", comparator="<", threshold=1, window_minutes=10)
    env = Env([rule])
    assert env.at(0)["quiet"]["state"] == "FIRING"          # 0 requests is a measurement here
    env.traffic(ok=1)
    assert env.at(1)["quiet"]["state"] == "OK"


def test_ai_unpriced_and_cost_rules():
    from obsdash.ai_telemetry import AITelemetry, OperationKind
    from obsdash.pricing import PricingTable
    rules = [dict(id="unp", name="U", metric="ai_unpriced_calls", comparator=">", threshold=0),
             dict(id="cost", name="C", metric="ai_cost", comparator=">", threshold=1.0, currency="USD")]
    env = Env(rules)
    assert env.at(0)["cost"]["state"] == "NO_DATA"          # no AI calls: cost undefined
    ai = AITelemetry(env.app.state.tracer_provider, env.metrics, PricingTable.from_dict({"models": [
        {"provider": "p", "model": "m", "input_per_million": "1000000", "output_per_million": "0"}]}))
    with ai.operation(OperationKind.MODEL_CALL, provider="p", target="m") as op:
        op.set_usage(input_tokens=2, output_tokens=0)       # cost = 2.0 USD
    with ai.operation(OperationKind.MODEL_CALL, provider="p", target="unknown") as op:
        op.set_usage(input_tokens=1, output_tokens=1)
    snap = env.at(1)
    assert snap["cost"]["state"] == "FIRING" and snap["unp"]["state"] == "FIRING"


# ---------------- persistence ----------------
def test_firing_state_survives_restart_without_renotifying(tmp_path):
    db = str(tmp_path / "alerts.db")
    env = Env([{**ERR, "for_seconds": 0}], alert_store=AlertStateStore(db))
    env.traffic(ok=2, fail=8)
    env.at(0)
    assert len(env.notifier.sent) == 1
    env.alert_store.close()
    # "restart": a brand-new evaluator + store object over the same file
    notifier2 = RecordingNotifier()
    ev2 = AlertEvaluator(env.ev.rules, env.store, AlertStateStore(db), Metrics(), notifier2,
                         clock=lambda: env.t0 + 5 * NS)
    ev2.evaluate_once()
    assert [r["state"] for r in ev2.snapshot()["rules"]] == ["FIRING"]
    assert notifier2.sent == []


def test_event_history_is_bounded():
    store = AlertStateStore(":memory:", max_events=3)
    for i in range(10):
        store.add_event(i, "r", "OK", "PENDING", 1.0, f"e{i}")
    ev = store.events(100)
    assert len(ev) == 3 and ev[0]["message"] == "e9"


def test_state_for_removed_rules_is_cleaned_up():
    store = AlertStateStore(":memory:")
    base = {"state": "OK", "since_ns": 1, "pending_since_ns": None, "last_value": None, "last_eval_ns": 1,
            "no_data": False, "message": ""}
    store.save_state("keep", base); store.save_state("gone", base)
    store.delete_unknown({"keep"})
    assert set(store.get_states()) == {"keep"}


# ---------------- failure handling ----------------
def test_webhook_delivery_to_a_real_local_receiver():
    received = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200); self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = Env([{**ERR, "for_seconds": 0}],
                  notifier=WebhookNotifier(f"http://127.0.0.1:{srv.server_port}/hook", Metrics()))
        env.traffic(ok=2, fail=8)
        env.at(0)
    finally:
        srv.shutdown()
    assert len(received) == 1 and received[0]["status"] == "firing" and received[0]["rule"] == "err"


def test_dead_webhook_is_counted_and_never_breaks_evaluation():
    metrics = Metrics()
    env = Env([{**ERR, "for_seconds": 0}], notifier=WebhookNotifier("http://127.0.0.1:1/hook", metrics, timeout_s=1))
    env.traffic(ok=2, fail=8)
    assert env.at(0)["err"]["state"] == "FIRING"            # state still recorded
    samples = [s for f in text_string_to_metric_families(metrics.render()[0].decode()) for s in f.samples]
    assert next(s.value for s in samples if s.name == "obsdash_alert_notification_failures_total") == 1


def test_webhook_url_must_be_http():
    with pytest.raises(ValueError):
        WebhookNotifier("file:///etc/passwd", Metrics())


def test_one_failing_rule_does_not_stop_the_others(monkeypatch):
    env = Env([{**ERR, "id": "bad", "for_seconds": 0}, {**ERR, "id": "good", "for_seconds": 0}])
    env.traffic(ok=2, fail=8)
    real = alert_engine.metric_value

    def flaky(rule, *a, **k):
        if rule.id == "bad":
            raise RuntimeError("boom")
        return real(rule, *a, **k)

    monkeypatch.setattr(alert_engine, "metric_value", flaky)
    snap = env.at(0)
    assert snap["good"]["state"] == "FIRING" and snap["bad"]["state"] == "NOT_EVALUATED"
    assert env.ev.last_error == "bad: RuntimeError"


def test_background_loop_survives_an_exception():
    env = Env([ERR])
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first pass dies")

    env.ev.evaluate_once = flaky

    async def run():
        task = asyncio.create_task(env.ev.run_forever(0.01))
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert len(calls) >= 3 and env.ev.last_error == "loop: RuntimeError"


def test_app_runs_the_evaluator_in_its_lifespan(tmp_path, monkeypatch):
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"rules": [rule_dict(id="q", metric="request_count", comparator="<", threshold=1)]}))
    monkeypatch.setenv("ALERT_RULES_PATH", str(rules))
    from obsdash.config import get_settings
    get_settings.cache_clear()
    try:
        store = TraceStore(":memory:")
        app = create_app(TracerProvider(), store, AlertStateStore(":memory:"))
        with TestClient(app):
            for _ in range(100):
                if app.state.alerts.last_run_ns is not None:
                    break
                time.sleep(0.02)
            assert app.state.alerts.last_run_ns is not None
    finally:
        monkeypatch.delenv("ALERT_RULES_PATH")
        get_settings.cache_clear()


def test_invalid_rules_file_stops_app_startup(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"rules": [rule_dict(metric="nope")]}))
    monkeypatch.setenv("ALERT_RULES_PATH", str(bad))
    from obsdash.config import get_settings
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="alert rules invalid"):
            create_app(TracerProvider(), TraceStore(":memory:"), AlertStateStore(":memory:"))
    finally:
        monkeypatch.delenv("ALERT_RULES_PATH")
        get_settings.cache_clear()


# ---------------- API + metrics ----------------
def test_alerts_api_and_prometheus_gauge():
    env = Env([{**ERR, "for_seconds": 0}])
    env.app.state.alerts = env.ev  # the API reads app.state.alerts
    c = env.client
    before = c.get("/api/alerts").json()
    assert before["rules"][0]["state"] == "NOT_EVALUATED" and before["firing"] == 0
    env.traffic(ok=2, fail=8)
    env.at(0)
    body = c.get("/api/alerts").json()
    assert body["firing"] == 1 and body["rules"][0]["state"] == "FIRING" and body["events"][0]["to_state"] == "FIRING"
    assert body["evaluator"]["last_run_ns"] is not None
    assert c.get("/api/alerts?event_limit=0").status_code == 422
    samples = [s for f in text_string_to_metric_families(c.get("/metrics").text) for s in f.samples]
    assert next(s.value for s in samples if s.name == "obsdash_alert_firing" and s.labels["rule"] == "err") == 1
    assert any(s.name == "obsdash_alert_last_evaluation_timestamp_seconds" and s.value > 0 for s in samples)


def test_alert_api_polling_does_not_pollute_the_trace_store():
    env = Env([ERR])
    env.app.state.alerts = env.ev
    n = env.store.span_count()
    env.client.get("/api/alerts")
    assert env.store.span_count() == n
