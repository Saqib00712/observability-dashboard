"""
Phase 11: Project 1 (production-rag) adapter.

What production-rag REALLY emits (read from its source, not assumed): when
`otel_enabled=true`, `app/observability/tracing.py::span(name, **attrs)`
opens a plain OTel span (kind INTERNAL, service.name "production-rag") for
these stage names only: retrieval, rerank, generation, generation_stream,
embedding (plus contextualize / memory_retrieve / memory_extract, which are
not model-billed stages and are left as ordinary spans). Its attributes are
app-specific (`chunks`, `sources`, `document_id`, `candidates`). It emits NO
token counts, NO model name, and NO HTTP server spans.

obsdash's AI views look for CLIENT spans carrying `gen_ai.operation.name`.
This module bridges that gap WITHOUT modifying production-rag: at ingest
time it tags the five stage spans accordingly. Nothing is invented:
  * no token or cost figures are added -- model/embedding stages are marked
    cost_status="no_usage", so the dashboard counts them as UNPRICED
    (unknown), never as free;
  * no provider/model is guessed (they show as "unknown").
Every enriched span carries `obsdash.enriched_by` so it is visible in the
trace view that the classification came from obsdash, not the sender.
"""

SERVICE_NAME = "production-rag"
ENRICHER_ID = "production_rag_adapter"

# production-rag span name -> obsdash OperationKind value
STAGE_TO_OPERATION = {
    "retrieval": "retrieval",
    "rerank": "rerank",
    "embedding": "embedding",
    "generation": "model_call",
    "generation_stream": "model_call",
}
_BILLED = ("model_call", "embedding")


def enrich(service: str, name: str, kind: str, attrs: dict) -> tuple[str, dict]:
    """Return (kind, attrs), possibly updated. Spans that already declare
    `gen_ai.operation.name` are never touched."""
    if service != SERVICE_NAME or "gen_ai.operation.name" in attrs:
        return kind, attrs
    op = STAGE_TO_OPERATION.get(name)
    if op is None:
        return kind, attrs
    out = dict(attrs)
    out["gen_ai.operation.name"] = op
    out["obsdash.ai.target"] = SERVICE_NAME
    out["obsdash.enriched_by"] = ENRICHER_ID
    if op in _BILLED:
        out["obsdash.ai.cost_status"] = "no_usage"
    return "CLIENT", out
