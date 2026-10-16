# Phase 10 — Security, privacy, performance

## What was built (and where)
| Area | Change | Files |
|---|---|---|
| Access control | Optional `API_TOKEN` (>= 16 chars). When set, every `/api/*` route and `/metrics` need `Authorization: Bearer <token>` or `X-API-Key`; constant-time compare; token never accepted in the URL; `/health` + the static dashboard shell stay open. Dashboard prompts for the token (kept in `sessionStorage` only). | `auth.py`, `config.py`, `api_*.py`, `static/dashboard.html` |
| Network exposure | Default `HOST` is now `127.0.0.1`. A warning is logged if you bind a non-loopback host with no token. | `config.py`, `main.py` |
| Redaction (logs, spans, store) | Bearer/Basic tokens, provider API keys, JWTs, AWS keys, `password=`/`api_key=` pairs (incl. JSON), e-mails, Luhn-valid card numbers. Applied to log messages/extras/error text, span names, attributes, event attributes, status text, request paths. | `redaction.py`, `logging_setup.py` |
| Redaction on EXPORT | Every exporter obsdash wires (console, OTLP, local store) is wrapped in `RedactingSpanExporter`, so a raw secret set on a span by any code is scrubbed before leaving the process. | `span_redaction.py`, `otel_setup.py` |
| Exceptions on spans | No `record_exception()` (it ships stack trace + raw message). Event keeps exception TYPE + redacted 200-char message only. | `tracing.py` |
| Bounded labels | Unknown routes -> one label; HTTP method outside the standard set -> `OTHER`; AI `(provider, target)` capped at 100 distinct pairs, overflow -> `__other__` and counted in `obsdash_label_overflow_total`; labels stripped of control chars, 64 chars max. | `metrics.py`, `ai_telemetry.py` |
| Input hardening | `X-Request-ID` only accepted if `[A-Za-z0-9._-]{1,64}`, else replaced. | `middleware.py` |
| Response hardening | `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` everywhere; `Cache-Control: no-store` on API/metrics; dashboard gets a strict CSP (script/style allowed only by SHA-256 hash of the inline blocks, `default-src 'none'`, `connect-src 'self'`, `frame-ancestors 'none'`). | `middleware.py`, `main.py` |
| Retention | Age-based (`TRACE_RETENTION_HOURS`, default 168) on top of the span-count cap; alert history capped. | `trace_store.py` |
| Timeouts / limits | Store queries abort after `QUERY_TIMEOUT_SECONDS` (default 5) -> HTTP 503 `query_timeout`; SQLite busy timeout; WAL mode on file DBs; webhook 3 s; OTLP exporter 5 s; background export queue bounded (2048 spans, 512/batch, 1 s flush). | `trace_store.py`, `otel_setup.py`, `alert_engine.py` |
| Export failure handling | Store failures return FAILURE, never raise into a request, and increment `obsdash_trace_export_failures_total`. | `trace_store.py` |
| Secrets in config | `api_token` and `alert_webhook_url` are `SecretStr` (not in `repr`); OTLP endpoint logged as scheme://host only. | `config.py`, `otel_setup.py` |

## Concepts
- **Defence in depth for privacy:** (1) don't record content (no prompt/response setters; headers/bodies never stored) -> (2) redact what slips through -> (3) redact again at export.
- **Constant-time comparison** stops attackers learning a token one character at a time from response timing.
- **CSP with hashes:** the browser only runs the exact inline script we ship; an injected script has a different hash and is refused.
- **Cardinality attack:** letting a caller choose label values lets them create unlimited time series (memory/disk exhaustion).
- **Non-blocking export:** request threads only enqueue spans; a background thread writes them.

## Commands
```
python -m pytest -q
python scripts/bench_overhead.py 1000
set API_TOKEN=pick-a-long-random-value-here      (PowerShell: $env:API_TOKEN="pick-a-long-random-value-here")
python -m uvicorn obsdash.main:app --port 8100
curl -H "Authorization: Bearer pick-a-long-random-value-here" http://localhost:8100/api/overview
```

## Results
192 passed (143 + 49 new), including: canary secrets (API key, password, e-mail, card, bearer token, cookie) injected through path, query, headers, exception text, span attributes and AI labels and then searched for in logs, spans, SQLite, `/metrics` and every API response (none found; the failure itself is still recorded); 1,000 distinct URLs -> 1 metric series; 500 AI targets -> 101 series; auth on all 6 protected routes (missing/wrong/header/query-string cases); CSP hash matches the page; retention deletes old spans and keeps recent ones; a 20,000-span query aborts with 503 and the store recovers; a slow exporter (1 s per write) did not slow 40 requests (< 0.8 s total).
Browser check (Chromium, token-protected server, strict CSP): token prompt appears, wrong token rejected, right token loads data, trace waterfall renders, zero CSP/JS console errors.
Overhead (indicative, TestClient, this sandbox): ~0.9 ms/request over a bare FastAPI handler (2.3 ms -> 3.3 ms).

## Honest limits
- Redaction is pattern-based. NOT detected: names, addresses, phone numbers, and secrets with no recognisable shape. Don't rely on it instead of not recording content.
- One shared token: no users, roles, rotation, or rate limiting; no TLS (use a reverse proxy). Token in `sessionStorage` is readable by any script in that origin, which is why the CSP is strict.
- `/docs` and `/openapi.json` (FastAPI defaults) stay open: they reveal API shape, not data.
- Heavy-looking queries are aborted by timeout, not prevented; `API` limits (max window 7 days, max 200 rows) bound the rest.
- The age prune runs at most once a minute; deleting rows does not shrink the SQLite file (no VACUUM).
- Spans dropped when the export queue is full are dropped by the OTel SDK and are not counted by obsdash.
- Docker/host-level limits (memory, CPU, file permissions of the `.db`) are Phase 13.
- The benchmark is in-process and indicative; it is not a load test.
