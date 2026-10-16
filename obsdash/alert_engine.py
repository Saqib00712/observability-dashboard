"""
Phase 9: the alert evaluator -- state machine, persistence, notification.

STATES (per rule)
  OK        value measured, not breaching.
  PENDING   breaching, but not yet for `for_seconds` (transient spikes end
            here and NEVER notify).
  FIRING    breached continuously for `for_seconds`. Notifies once.
  NO_DATA   the metric can't be computed (no requests in the window, too
            few samples, no telemetry at all). Deliberately NOT "OK": we
            can't claim health without evidence, and not "FIRING": an idle
            service isn't failing. (Use the `request_count` and
            `telemetry_staleness_seconds` metrics to alert on silence.)

TRANSITIONS
  OK/NO_DATA -> PENDING (or FIRING if for_seconds == 0) when breaching
  PENDING    -> FIRING after for_seconds of continuous breach
  PENDING    -> OK when the breach ends (transient: suppressed)
  FIRING     -> OK when no longer breaching (RESOLVED, notifies once)
  FIRING + no data -> stays FIRING, flagged no_data. We never auto-resolve
  without evidence of recovery.
  anything else + no data -> NO_DATA (pending timer resets)

Notification is fail-soft: a dead webhook is logged and counted, never
raised into the evaluator. State is persisted to SQLite so a restart does
not forget a FIRING alert (and does not re-notify for it).

What this is NOT: Prometheus Alertmanager. No grouping/silencing/routing/
escalation; one optional webhook. Evaluation runs inside the obsdash
process, so if obsdash is down, nothing alerts -- the evaluator's own
liveness is exposed as `obsdash_alert_last_evaluation_timestamp_seconds`
for an EXTERNAL system (e.g. Prometheus) to watch.
"""

import asyncio
import json
import logging
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

from obsdash.alert_rules import SAMPLED_METRICS, AlertRule
from obsdash.analytics import build_overview
from obsdash.metrics import Metrics
from obsdash.trace_store import TraceStore

logger = logging.getLogger("obsdash.alerts")

OK, PENDING, FIRING, NO_DATA = "OK", "PENDING", "FIRING", "NO_DATA"
_NS = 1_000_000_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_state (
    rule_id TEXT PRIMARY KEY, state TEXT NOT NULL, since_ns INTEGER NOT NULL,
    pending_since_ns INTEGER, last_value REAL, last_eval_ns INTEGER NOT NULL,
    no_data INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ns INTEGER NOT NULL, rule_id TEXT NOT NULL,
    from_state TEXT, to_state TEXT NOT NULL, value REAL, message TEXT NOT NULL
);
"""


class AlertStateStore:
    """Alert state + bounded transition history. Own connection, opened lazily."""

    def __init__(self, path: str = "obsdash.db", max_events: int = 1000, busy_timeout_s: float = 5.0) -> None:
        self._path, self._max_events, self._busy = path, max_events, busy_timeout_s
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self._path, check_same_thread=False, timeout=self._busy)
            conn.row_factory = sqlite3.Row
            if self._path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            self._conn = conn
        return self._conn

    def get_states(self) -> dict[str, dict]:
        with self._lock:
            return {r["rule_id"]: dict(r) for r in self._db().execute("SELECT * FROM alert_state")}

    def save_state(self, rule_id: str, st: dict) -> None:
        with self._lock:
            db = self._db()
            db.execute(
                "INSERT OR REPLACE INTO alert_state (rule_id, state, since_ns, pending_since_ns, last_value, "
                "last_eval_ns, no_data, message) VALUES (?,?,?,?,?,?,?,?)",
                (rule_id, st["state"], st["since_ns"], st["pending_since_ns"], st["last_value"],
                 st["last_eval_ns"], int(st["no_data"]), st["message"]))
            db.commit()

    def add_event(self, ts_ns: int, rule_id: str, from_state: str | None, to_state: str,
                  value: float | None, message: str) -> None:
        with self._lock:
            db = self._db()
            db.execute("INSERT INTO alert_events (ts_ns, rule_id, from_state, to_state, value, message) "
                       "VALUES (?,?,?,?,?,?)", (ts_ns, rule_id, from_state, to_state, value, message))
            db.execute("DELETE FROM alert_events WHERE id <= (SELECT MAX(id) FROM alert_events) - ?",
                       (self._max_events,))
            db.commit()

    def events(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db().execute("SELECT * FROM alert_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def delete_unknown(self, known_ids: set[str]) -> None:
        with self._lock:
            db = self._db()
            for (rid,) in db.execute("SELECT rule_id FROM alert_state").fetchall():
                if rid not in known_ids:
                    db.execute("DELETE FROM alert_state WHERE rule_id = ?", (rid,))
            db.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


class WebhookNotifier:
    """POSTs JSON to one URL. Short timeout, no retries, never raises."""

    def __init__(self, url: str, metrics: Metrics, timeout_s: float = 3.0) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError("ALERT_WEBHOOK_URL must start with http:// or https://")
        self._url, self._metrics, self._timeout = url, metrics, timeout_s

    def notify(self, payload: dict) -> bool:
        try:
            req = urllib.request.Request(
                self._url, data=json.dumps(payload).encode(), method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "obsdash-alerts"})
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 -- operator-configured URL
                ok = 200 <= resp.status < 300
            if not ok:
                raise RuntimeError(f"webhook returned HTTP {resp.status}")
            return True
        except Exception as exc:  # noqa: BLE001 -- notification must fail soft
            self._metrics.alert_notification_failures_total.inc()
            logger.error("alert_notification_failed", extra={"error_type": type(exc).__name__})
            return False


def metric_value(rule: AlertRule, overview: dict, now_ns: int, newest_ns: int | None) -> tuple[float | None, str]:
    """(value, reason). value None => cannot be computed (reason says why)."""
    m = rule.metric
    req, ai = overview["requests"], overview["ai"]
    if m == "telemetry_staleness_seconds":
        return (None, "no_telemetry_ever") if newest_ns is None else (max(0.0, (now_ns - newest_ns) / _NS), "")
    if m == "request_count":
        return float(req["total"]), ""
    if m == "ai_unpriced_calls":
        return float(ai["unpriced_calls"]), ""
    if m == "ai_cost":
        return (None, "no_ai_calls") if ai["calls"] == 0 else (float(ai["cost_by_currency"].get(rule.currency, 0.0)), "")

    samples = req["total"] if m.startswith("request_") else ai["calls"]
    if samples == 0:
        return None, "no_samples"
    if rule.min_samples and samples < rule.min_samples:
        return None, f"insufficient_samples({samples}<{rule.min_samples})"
    if m == "request_error_rate":
        return float(req["error_rate"]), ""
    if m == "request_latency_p95_ms":
        return float(req["latency_ms"]["p95"]), ""
    if m == "request_latency_p99_ms":
        return float(req["latency_ms"]["p99"]), ""
    if m == "ai_error_rate":
        return ai["errors"] / ai["calls"], ""
    raise ValueError(f"unsupported metric {m}")  # unreachable: validated by AlertRule


assert SAMPLED_METRICS  # keep the import honest: documents which metrics are sample-based


class AlertEvaluator:
    def __init__(self, rules: list[AlertRule], trace_store: TraceStore, state_store: AlertStateStore,
                 metrics: Metrics, notifier: WebhookNotifier | None = None,
                 clock: Callable[[], int] = time.time_ns) -> None:
        self.rules = rules
        self._traces, self._store, self._metrics, self._notifier, self._clock = trace_store, state_store, metrics, notifier, clock
        self._lock = threading.Lock()
        self.last_run_ns: int | None = None
        self.last_error: str | None = None

    # ---- one evaluation pass ----
    def evaluate_once(self) -> None:
        with self._lock:  # never two passes at once
            now = self._clock()
            states = self._store.get_states()
            overviews: dict[int, dict] = {}
            bounds = self._traces.bounds()
            for rule in self.rules:
                try:
                    if rule.window_minutes not in overviews:
                        rows = self._traces.query_spans(since_ns=now - rule.window_minutes * 60 * _NS)
                        overviews[rule.window_minutes] = build_overview(rows, rule.window_minutes, now, bounds)
                    value, reason = metric_value(rule, overviews[rule.window_minutes], now, bounds["newest_ns"])
                    self._step(rule, states.get(rule.id), value, reason, now)
                except Exception as exc:  # noqa: BLE001 -- one bad rule must not stop the others
                    self._metrics.alert_evaluation_failures_total.inc()
                    self.last_error = f"{rule.id}: {type(exc).__name__}"
                    logger.error("alert_rule_evaluation_failed", extra={"rule": rule.id, "error_type": type(exc).__name__})
            self.last_run_ns = now
            self._metrics.alert_last_evaluation_timestamp.set(now / _NS)

    def _step(self, rule: AlertRule, prev: dict | None, value: float | None, reason: str, now: int) -> None:
        prev_state = prev["state"] if prev else None
        pending_since = prev["pending_since_ns"] if prev else None
        no_data = value is None

        if no_data:
            if prev_state == FIRING:
                new, pending_since = FIRING, None  # no evidence of recovery -> stay firing
                msg = f"no data ({reason}); still FIRING until recovery is observed"
            else:
                new, pending_since, msg = NO_DATA, None, f"no data: {reason}"
        elif rule.breached(value):
            msg = f"{rule.metric} = {_fmt(value)} {rule.comparator} {_fmt(rule.threshold)}"
            if prev_state == FIRING:
                new = FIRING
            elif prev_state == PENDING and pending_since is not None:
                new = FIRING if now - pending_since >= rule.for_seconds * _NS else PENDING
            else:
                pending_since = now
                new = FIRING if rule.for_seconds == 0 else PENDING
            if new == FIRING:
                pending_since = None
        else:
            new, pending_since = OK, None
            msg = f"{rule.metric} = {_fmt(value)} (ok, threshold {rule.comparator} {_fmt(rule.threshold)})"

        changed = new != prev_state
        since = now if changed else prev["since_ns"]
        self._store.save_state(rule.id, {
            "state": new, "since_ns": since, "pending_since_ns": pending_since,
            "last_value": value if value is not None else (prev["last_value"] if prev else None),
            "last_eval_ns": now, "no_data": no_data, "message": msg})
        self._metrics.alert_firing.labels(rule.id, rule.severity).set(1 if new == FIRING else 0)

        if changed and not (prev_state is None and new in (OK, NO_DATA)):
            self._store.add_event(now, rule.id, prev_state, new, value, msg)
            logger.log(logging.WARNING if new == FIRING else logging.INFO, "alert_transition",
                       extra={"rule": rule.id, "from_state": prev_state, "to_state": new, "value": value})
            if new == FIRING and prev_state != FIRING:
                self._notify("firing", rule, value, msg, now)
            elif prev_state == FIRING and new == OK:
                self._notify("resolved", rule, value, msg, now)

    def _notify(self, status: str, rule: AlertRule, value: float | None, msg: str, now: int) -> None:
        if self._notifier is None:
            return
        self._notifier.notify({
            "status": status, "rule": rule.id, "name": rule.name, "severity": rule.severity,
            "metric": rule.metric, "comparator": rule.comparator, "threshold": rule.threshold,
            "value": value, "window_minutes": rule.window_minutes, "message": msg,
            "timestamp": now / _NS})

    # ---- background loop ----
    async def run_forever(self, interval_s: float) -> None:
        while True:
            try:
                await asyncio.to_thread(self.evaluate_once)
            except Exception as exc:  # noqa: BLE001 -- the loop itself must survive anything
                self._metrics.alert_evaluation_failures_total.inc()
                self.last_error = f"loop: {type(exc).__name__}"
                logger.error("alert_loop_error", extra={"error_type": type(exc).__name__})
            await asyncio.sleep(interval_s)

    # ---- read side ----
    def snapshot(self, event_limit: int = 50) -> dict:
        states = self._store.get_states()
        rules = []
        for r in self.rules:
            st = states.get(r.id)
            rules.append({
                "id": r.id, "name": r.name, "metric": r.metric, "comparator": r.comparator,
                "threshold": r.threshold, "window_minutes": r.window_minutes, "for_seconds": r.for_seconds,
                "min_samples": r.min_samples, "severity": r.severity, "description": r.description,
                "state": st["state"] if st else "NOT_EVALUATED",
                "since_ns": st["since_ns"] if st else None,
                "last_value": st["last_value"] if st else None,
                "no_data": bool(st["no_data"]) if st else False,
                "message": st["message"] if st else "not evaluated yet",
            })
        return {
            "rules": rules,
            "firing": sum(1 for x in rules if x["state"] == FIRING),
            "events": self._store.events(event_limit),
            "evaluator": {"last_run_ns": self.last_run_ns, "last_error": self.last_error},
        }


def _fmt(v: float) -> str:
    return f"{v:.4g}"
