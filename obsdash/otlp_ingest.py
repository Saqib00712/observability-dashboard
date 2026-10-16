"""
Phase 11: OTLP/HTTP trace receiver -- `POST /v1/traces`.

Why this is THE integration interface: OpenTelemetry's OTLP is a stable,
published wire protocol. Any app that can export OTLP (Project 1 already
can, optionally) sends spans to obsdash by setting an endpoint, with no
obsdash-specific code in that app. That is better than inventing a custom
ingestion API that every project would have to learn.

Supported: OTLP/HTTP, protobuf body (`Content-Type: application/x-protobuf`),
optional gzip. NOT supported (stated, not hidden): OTLP/JSON, OTLP/gRPC,
metrics and logs signals -- only traces.

Bounded and safe, because an ingestion endpoint faces other programs:
  * token auth (same API_TOKEN as every other endpoint; exporters send it
    via OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <token>");
  * body cap (compressed AND decompressed), span-count cap per request;
  * malformed spans are dropped and counted, never stored;
  * every stored value goes through the same redaction/truncation as
    locally created spans (sanitize_attributes, redact_text);
  * a storage failure answers 503 so the exporter retries.
"""

import json
import zlib

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as pb
from google.protobuf.message import DecodeError

from obsdash.auth import require_token
from obsdash.integrations import production_rag
from obsdash.redaction import redact_text
from obsdash.span_redaction import sanitize_attributes

router = APIRouter(dependencies=[Depends(require_token)])

_KINDS = {1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
_STATUS = {0: "UNSET", 1: "OK", 2: "ERROR"}
_MAX_ATTRS = 64
_PROTOBUF = "application/x-protobuf"


def _scalar(v):
    which = v.WhichOneof("value")
    if which in ("string_value", "bool_value", "int_value", "double_value"):
        return getattr(v, which)
    return None  # arrays / maps / bytes are not stored


def _attrs(kvs) -> dict:
    out = {}
    for kv in kvs:
        if len(out) >= _MAX_ATTRS:
            break
        val = _scalar(kv.value)
        if val is not None:
            out[kv.key] = val
    return out


def spans_to_rows(req: "pb.ExportTraceServiceRequest", enrich_known: bool = True) -> tuple[list[tuple], int]:
    """Wire format -> store rows. Returns (rows, rejected_count)."""
    rows, rejected = [], 0
    for rs in req.resource_spans:
        service = redact_text(str(_attrs(rs.resource.attributes).get("service.name", "unknown")), 100)
        for ss in rs.scope_spans:
            for sp in ss.spans:
                if len(sp.trace_id) != 16 or len(sp.span_id) != 8 or not any(sp.trace_id) or not any(sp.span_id) \
                        or sp.start_time_unix_nano == 0:
                    rejected += 1
                    continue
                kind = _KINDS.get(sp.kind, "INTERNAL")
                attrs = _attrs(sp.attributes)
                if enrich_known:
                    kind, attrs = production_rag.enrich(service, sp.name, kind, attrs)
                start = sp.start_time_unix_nano
                end = max(sp.end_time_unix_nano, start)  # a missing/backwards end is clamped, not trusted
                rows.append((
                    sp.trace_id.hex(), sp.span_id.hex(), sp.parent_span_id.hex() if sp.parent_span_id else None,
                    redact_text(sp.name, 200), kind, service, start, end,
                    _STATUS.get(sp.status.code, "UNSET"),
                    json.dumps(sanitize_attributes(attrs), default=str),
                    redact_text(sp.status.message or "", 200) or None,
                ))
    return rows, rejected


def _read_limited(raw: bytes, encoding: str, limit: int) -> bytes | None:
    """Decode the body; None means 'too large' (either form)."""
    if len(raw) > limit:
        return None
    if encoding in ("", "identity"):
        return raw
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(raw, limit + 1)
    if len(out) > limit or d.unconsumed_tail:
        return None  # decompression-bomb guard
    return out


def _err(status: int, code: str, metrics, outcome: str, headers=None) -> JSONResponse:
    metrics.otlp_requests_total.labels(outcome).inc()
    return JSONResponse(status_code=status, content={"error": code}, headers=headers)


@router.post("/v1/traces", include_in_schema=False)
async def receive_traces(request: Request) -> Response:
    app = request.app
    metrics = app.state.metrics
    limit = app.state.otlp_max_body_bytes

    if not request.headers.get("content-type", "").lower().startswith(_PROTOBUF):
        return _err(415, "unsupported_media_type: send application/x-protobuf", metrics, "unsupported")
    encoding = request.headers.get("content-encoding", "").strip().lower()
    if encoding not in ("", "identity", "gzip"):
        return _err(415, "unsupported_content_encoding", metrics, "unsupported")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return _err(413, "body_too_large", metrics, "too_large")

    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return _err(413, "body_too_large", metrics, "too_large")
        chunks.append(chunk)
    try:
        body = _read_limited(b"".join(chunks), encoding, limit)
    except zlib.error:
        return _err(400, "bad_gzip", metrics, "bad_request")
    if body is None:
        return _err(413, "body_too_large", metrics, "too_large")

    msg = pb.ExportTraceServiceRequest()
    try:
        msg.ParseFromString(body)
    except DecodeError:
        return _err(400, "bad_protobuf", metrics, "bad_request")

    total = sum(len(ss.spans) for rs in msg.resource_spans for ss in rs.scope_spans)
    if total > app.state.otlp_max_spans_per_request:
        return _err(413, "too_many_spans", metrics, "too_large")

    rows, rejected = spans_to_rows(msg, app.state.otlp_enrich_known)
    try:
        await run_in_threadpool(app.state.trace_store.add_rows, rows)
    except Exception:  # noqa: BLE001 -- answer 503 so the exporter retries
        metrics.trace_export_failures_total.inc()
        return _err(503, "storage_unavailable", metrics, "storage_error", {"Retry-After": "5"})

    metrics.otlp_spans_total.labels("accepted").inc(len(rows))
    if rejected:
        metrics.otlp_spans_total.labels("rejected").inc(rejected)
    metrics.otlp_requests_total.labels("partial" if rejected else "accepted").inc()
    resp = pb.ExportTraceServiceResponse()
    if rejected:
        resp.partial_success.rejected_spans = rejected
        resp.partial_success.error_message = "invalid span ids or timestamps"
    return Response(resp.SerializeToString(), media_type=_PROTOBUF)
