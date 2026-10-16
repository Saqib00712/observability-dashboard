# Phase 4 — Metrics model and Prometheus

## What was built
- `obsdash/metrics.py` — `Metrics` class (own CollectorRegistry): counter `obsdash_http_requests_total{method,route,status_class}`, histogram `obsdash_http_request_duration_seconds{method,route}`, gauge `obsdash_http_requests_in_flight`.
- `obsdash/middleware.py` — records metrics in `finally` (crashes count as 5xx; in_flight never leaks).
- `obsdash/main.py` — `GET /metrics` in Prometheus text format.

## Concepts
- **Counter** only rises; Prometheus computes rate() from it. **Gauge** moves both ways. **Histogram** buckets observations so p95 can be computed (`histogram_quantile`).
- **Pull model**: Prometheus scrapes `/metrics` on an interval; the app never pushes.
- **Cardinality**: each distinct label combo = one stored time series. Labels use the route TEMPLATE (`/items/{id}`), never raw paths; unknown URLs collapse to `unmatched`. request_id/trace_id are never labels.
- Metrics say THAT something is slow/failing; traces say WHICH request; logs say WHY.

## Commands
```
pip install -r requirements-dev.txt
python -m pytest -q
python -m uvicorn obsdash.main:app --port 8100
curl http://localhost:8100/health
curl http://localhost:8100/metrics
```
(On Windows without curl output filtering, just read the whole /metrics text.)

## Results
34 passed (27 + 7 new). Live check: 2x /health + 1x /nope gave requests_total 2 (2xx, /health) and 1 (4xx, unmatched).

## Honest limits
- `in_flight` shows 1 while scraping because the /metrics request counts itself.
- /metrics is recorded in its own metrics (usual Prometheus behavior); it is unauthenticated until Phase 10.
- No real Prometheus server was run here; format is verified by parsing it with prometheus_client's own parser.
- Span attribute `http.route` still holds the raw path (span names are not metric labels); revisit in Phase 6.
- OTel metrics SDK not used; Prometheus client is the single metrics path for now.
