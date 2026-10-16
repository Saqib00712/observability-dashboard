# Phase 8 — Dashboard API and frontend

## What was built
- `GET /dashboard` — single-page dashboard served by obsdash itself (`obsdash/static/dashboard.html`; no build step, no CDN, works offline). `GET /` redirects to it.
- Pages: **Overview** (requests, error rate, p50/p95/p99, model calls, cost; requests-per-bucket chart, p95 chart, routes table), **AI & cost** (tokens, cost, per-operation and per-model tables, unpriced warning), **Traces** (list, errors-only filter, waterfall with attributes), **Errors** (grouped + recent, click opens the trace).
- `GET /api/overview?window_minutes=` and `GET /api/errors?window_minutes=&limit=` (`obsdash/api_dashboard.py`), math in `obsdash/analytics.py`. Existing `/api/traces` and `/api/costs/summary` feed the other pages.
- `scripts/seed_demo_data.py` — pushes SYNTHETIC traffic through the real instrumentation into a SQLite file for demos.
- Span names / `http.route` now use the route TEMPLATE (`GET /items/{id}`), not the raw path, so dashboards stay bounded.
- Store: new `status_message` column (auto-migrates databases from Phases 6–7).

## Where each number comes from (no hardcoded values)
Everything is computed from spans in the trace store (Phase 6), which the real middleware and AI wrapper write. Not from Prometheus (that has no time history).
- request = SERVER span; error = ERROR status (exception or 5xx). 404/422 are NOT service errors.
- Latency percentiles: nearest-rank over the window.
- "Model calls" = model_call + embedding spans only; retrieval/rerank/tool/agent steps appear in the operations table.
- Cost: only priced calls; unpriced calls shown separately (Phase 7 rule).
- Errors list shows the ORIGIN of a failure, not the three parent spans it propagates through.

## Behaviour
- Empty store -> "No telemetry collected yet…". Data exists but not in window -> says so, with newest-span time.
- Backend failure/timeout (8 s) -> red banner, last good data kept and dimmed.
- Auto-refresh 5 s (paused when the tab is hidden); stale responses are discarded.
- All API text is inserted with `textContent` only (a test forbids `innerHTML`, `eval`, external URLs).
- Light/dark follow the OS; the Theme button overrides. Charts have hover tooltips and a table view; errors are marked with colour AND ✕ / text.

## Commands
```
python -m pytest -q
python scripts/seed_demo_data.py demo.db 200
set SQLITE_PATH=demo.db                      (PowerShell: $env:SQLITE_PATH="demo.db")
set PRICING_TABLE_PATH=pricing.example.json  (optional; PowerShell: $env:PRICING_TABLE_PATH="pricing.example.json")
python -m uvicorn obsdash.main:app --port 8100
```
Open http://localhost:8100/dashboard . The seeded data is synthetic; to see real data, point `SQLITE_PATH` at a store written by a running instrumented app.

## Results
101 passed (83 + 18 new). Browser check with Playwright/Chromium on seeded data: all four pages rendered, hover tooltip, trace waterfall, dark mode, empty-store message, injected backend 500 -> banner + dimmed data, 390 px width with no horizontal scroll, zero console errors. (Browser checks are manual, not part of the pytest suite.)

## Honest limits
- Window data comes from the bounded span store (50k spans): a busy system's long windows are truncated by the cap, and `/health` traffic is stored like any other request.
- No server-side sampling; no pagination beyond the limits shown; no custom time range.
- The dashboard has no authentication yet (Phase 10). Anyone who can reach the port can read traces.
- Seed script data is synthetic and says so in its output.
- Charts are hand-built SVG: no zoom, no legend toggling.
