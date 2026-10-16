# Phase 9 — Alerting

## What was built
- `obsdash/alert_rules.py` — rule model + strict validation. Rules come from JSON (`ALERT_RULES_PATH`, example: `alert_rules.example.json`). Unknown fields, bad metrics, NaN thresholds, duplicate ids, missing currency etc. stop the app at startup with ALL problems listed.
- `obsdash/alert_engine.py` — evaluator (state machine), SQLite state + bounded history, webhook notifier.
- `obsdash/api_alerts.py` — `GET /api/alerts` (rules + state, recent transitions, evaluator status).
- Dashboard **Alerts** tab (badge shows firing count); Prometheus: `obsdash_alert_firing{rule,severity}`, `obsdash_alert_evaluation_failures_total`, `obsdash_alert_notification_failures_total`, `obsdash_alert_last_evaluation_timestamp_seconds`.
- `scripts/demo_alerts.py` — full lifecycle demo (simulated clock, real everything else).
- Config: `ALERT_RULES_PATH`, `ALERT_EVAL_INTERVAL_SECONDS` (default 30, min 5), `ALERT_WEBHOOK_URL`, `ALERT_MAX_EVENTS`.

## Metrics a rule can watch
request_count, request_error_rate, request_latency_p95_ms, request_latency_p99_ms, ai_error_rate, ai_unpriced_calls, ai_cost (needs `currency`), telemetry_staleness_seconds. A rule = metric + comparator + threshold + window_minutes + for_seconds + min_samples + severity.

## States and transitions
| State | Meaning |
|---|---|
| OK | measured, not breaching |
| PENDING | breaching, but not yet for `for_seconds` |
| FIRING | breached continuously for `for_seconds` (notifies once) |
| NO_DATA | cannot be computed (no requests / too few samples / no telemetry at all) |

- OK or NO_DATA -> PENDING (FIRING at once if `for_seconds` is 0) -> FIRING after `for_seconds` of unbroken breach.
- PENDING -> OK when the breach ends: transient spikes never notify, and the timer restarts on the next breach.
- FIRING -> OK = RESOLVED (notifies once).
- **Missing telemetry:** NO_DATA is neither OK nor FIRING. A FIRING alert that loses its data STAYS FIRING (flagged no_data): recovery is never assumed without evidence. To alert on silence, use `request_count < 1` or `telemetry_staleness_seconds > N` (a store that never received anything is NO_DATA).
- `min_samples` stops "1 request, 1 failure = 100%" false alarms.

## Failure handling
- Webhook failure/timeout (3 s, no retries) is logged and counted, never raised; state is still recorded.
- One rule raising does not stop the others; the background loop survives any exception.
- State persists in SQLite, so a restart keeps FIRING alerts without re-notifying. State for rules removed from the file is deleted at startup.
- Evaluation runs in a worker thread, so it can't block request handling.

## Commands
```
python -m pytest -q
python scripts/demo_alerts.py
set ALERT_RULES_PATH=alert_rules.example.json          (PowerShell: $env:ALERT_RULES_PATH="alert_rules.example.json")
set ALERT_WEBHOOK_URL=http://localhost:9000/hook       (optional)
python -m uvicorn obsdash.main:app --port 8100
curl http://localhost:8100/api/alerts
```
Open http://localhost:8100/dashboard#alerts

## Results
143 passed (101 + 42 new). Lifecycle demo output (simulated clock): NO_DATA -> OK -> PENDING (t+10) -> PENDING (t+60, 50 s in) -> FIRING (t+75) -> RESOLVED (t+80) -> NO_DATA, and telemetry-stale FIRING at t+440; 3 real HTTP webhook deliveries received by a local listener (firing, resolved, firing). Live server with the example rules on seeded data evaluated every 5 s: `unpriced-calls` FIRING, two rules NO_DATA (insufficient samples), the rest OK; the Alerts tab matched.

## Honest limits
- Not Alertmanager: no grouping, silencing, routing, escalation, or repeat reminders; one optional webhook, no retries.
- Evaluation is inside the obsdash process: if obsdash is down, nothing alerts. Watch `obsdash_alert_last_evaluation_timestamp_seconds` from an external system for that.
- Rules load at startup only (restart to change them).
- Time in the lifecycle demo/tests is simulated; real waiting for `for_seconds` was not run end-to-end here.
- Each pass re-reads spans in the largest window (bounded by the 50k-span store); fine at the default 30 s interval, not a high-frequency evaluator.
- The webhook URL is operator-set configuration (not user input); restrict who can edit your environment.
- No hysteresis beyond `for_seconds`: a value hovering on the threshold can flip OK/PENDING.
