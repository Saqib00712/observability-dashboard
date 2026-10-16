# Phase 11 — Integrations (Project 1, Project 2; Project 3 contract only)

## What was found in the real code (nothing assumed)
- **production-rag** (Project 1): optional, fail-open OTel (`otel_enabled`, `otel_exporter_otlp_endpoint`).
  Its `tracing.span()` emits INTERNAL spans named `retrieval`, `rerank`, `generation`,
  `generation_stream`, `embedding` (+ `contextualize`, `memory_*`). **No** token counts, model
  names, or HTTP server spans.
- **eval-harness** (Project 2): `RunObserver` with six hooks, passed to `run_evaluation(observer=...)`.
- obsdash had **no ingestion endpoint**, so a separate process could not send it anything.

## What was built
1. `POST /v1/traces` — OTLP/HTTP protobuf receiver (`obsdash/otlp_ingest.py`). Token-protected,
   body cap (also after gunzip), span-count cap, invalid spans dropped + reported as OTLP
   `partial_success`, redaction identical to local spans, 503 on storage failure.
   Its own requests are not recorded as telemetry. Metrics: `obsdash_otlp_requests_total{outcome}`,
   `obsdash_otlp_spans_total{result}`.
2. Project 1 enricher (`obsdash/integrations/production_rag.py`): at ingest, tags the five AI stage
   spans as AI operations. Adds **no** tokens/cost/model; model + embedding stages are marked
   `no_usage` so they show as *unpriced*. Spans carry `obsdash.enriched_by`. Toggle:
   `OTLP_ENRICH_KNOWN_SERVICES=false`.
3. Project 2 observer (`obsdash/integrations/eval_harness_observer.py`): `ObsdashRunObserver` emits
   `eval_run > eval_test_case > {system_run, eval_evaluator}`. A failed threshold is **not** a span error;
   only a crash is. Never raises into the runner.
4. Project 3: `docs/integration_contract.md` only (project not built).
5. Defect found and fixed by this phase: trace list named the wrong root when a child shared its
   parent's start timestamp (now prefers the parentless span). Regression test added.

## Commands
```
# obsdash
python -m uvicorn obsdash.main:app --port 8100

# Project 1, unmodified, real app
OTEL_ENABLED=true OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8100/v1/traces   (set in its .env)
# (Windows cmd: set OTEL_ENABLED=true  &  set OTEL_EXPORTER_OTLP_ENDPOINT=...)
# or a no-OpenAI demo using its real tracing code:
python scripts/example_production_rag_to_obsdash.py --production-rag ../production-rag

# Project 2
python scripts/example_eval_to_obsdash.py --eval-harness ../eval-harness

# tests (sibling repos are found next to this one, or via OBSDASH_EVAL_HARNESS_PATH / OBSDASH_PRODUCTION_RAG_PATH)
python -m pytest -q
```
Install the new explicit dependency: `pip install -r requirements.txt` (adds `opentelemetry-proto`).

## Limits (honest)
- Only traces over OTLP/HTTP-protobuf. `/metrics` reflects obsdash's own process; remote spans appear in
  the dashboard/API/alerts (all derived from the store), not in Prometheus counters.
- production-rag has no HTTP server spans, so the dashboard's request panels stay empty for it; AI rows
  show unknown provider/model and unpriced cost.
- Remote span names are trusted as sent (the store cap bounds growth); local routes are templated.
- Eval evaluator durations are approximated from hook timing gaps.
- Not verified: obsdash receiving from a *running* production-rag server handling real `/ask` calls
  (needs OpenAI). The real tracing code was exercised in a separate process instead.
