"""
Alert lifecycle demo: healthy -> breach -> PENDING -> FIRING (webhook) ->
recovery -> RESOLVED -> telemetry goes silent -> NO DATA / stale.

Uses the REAL pipeline (requests -> spans -> SQLite -> analytics -> alert
engine -> a real HTTP webhook to a local listener). Only the CLOCK is
simulated, so the demo takes seconds instead of minutes. The requests
are synthetic.

    python scripts/demo_alerts.py
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402

from obsdash.alert_engine import AlertEvaluator, AlertStateStore, WebhookNotifier  # noqa: E402
from obsdash.alert_rules import parse_rules  # noqa: E402
from obsdash.main import create_app  # noqa: E402
from obsdash.trace_store import TraceStore, TraceStoreExporter  # noqa: E402

NS = 1_000_000_000
RULES = {"rules": [
    {"id": "high-error-rate", "name": "Error rate above 20%", "metric": "request_error_rate", "comparator": ">",
     "threshold": 0.2, "window_minutes": 5, "for_seconds": 60, "min_samples": 5, "severity": "critical"},
    {"id": "telemetry-stale", "name": "No telemetry for 5 min", "metric": "telemetry_staleness_seconds",
     "comparator": ">", "threshold": 300, "severity": "critical"},
]}


def main() -> None:
    received: list[dict] = []

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200); self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    store = TraceStore(":memory:")
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(TraceStoreExporter(store)))
    app = create_app(provider, store, AlertStateStore(":memory:"))

    @app.get("/ok")
    def ok():
        return {}

    @app.get("/boom")
    def boom():
        raise ValueError("simulated failure")

    client = TestClient(app, raise_server_exceptions=False)
    t0 = time.time_ns()
    offset = [0]
    notifier = WebhookNotifier(f"http://127.0.0.1:{server.server_port}/hook", app.state.metrics)
    ev = AlertEvaluator(parse_rules(RULES), store, AlertStateStore(":memory:"), app.state.metrics, notifier,
                        clock=lambda: t0 + offset[0] * NS)

    def step(label: str, at_s: int) -> None:
        offset[0] = at_s
        ev.evaluate_once()
        states = {r["id"]: r["state"] for r in ev.snapshot()["rules"]}
        print(f"t=+{at_s:>4}s  {label:<44} error-rate: {states['high-error-rate']:<8} stale: {states['telemetry-stale']}")

    print("clock is simulated; requests, spans, SQLite, rules and the webhook are real\n")
    step("no traffic yet", 0)
    for _ in range(30): client.get("/ok")
    step("30 healthy requests", 5)
    for _ in range(12): client.get("/boom")
    step("12 failures arrive (12/42 = 29%)", 10)
    step("still breaching, 50 s in (needs 60)", 60)
    step("breach lasted >= 60 s", 75)
    for _ in range(120): client.get("/ok")
    step("120 healthy requests dilute errors (8%)", 80)
    step("5+ minutes pass, nobody sends telemetry", 80 + 6 * 60)

    server.shutdown()
    print("\nwebhook deliveries received:")
    for p in received:
        print(f"  {p['status']:<9} rule={p['rule']:<16} value={p['value']:.3g}  ({p['message']})")
    print("\ntransition history:")
    for e in reversed(ev.snapshot()["events"]):
        print(f"  {e['rule_id']:<16} {str(e['from_state']):<8} -> {e['to_state']}")


if __name__ == "__main__":
    main()
