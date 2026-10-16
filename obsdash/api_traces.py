"""Phase 6: read API over the trace store (Phase 8 builds the UI on it)."""

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from obsdash.auth import require_token

router = APIRouter(prefix="/api/traces", tags=["traces"], dependencies=[Depends(require_token)])

_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")


@router.get("")
def list_traces(request: Request, limit: int = Query(20, ge=1, le=100), status: str | None = None) -> dict:
    if status not in (None, "error"):
        raise HTTPException(status_code=422, detail="status must be 'error' or omitted")
    traces = request.app.state.trace_store.list_traces(limit=limit, errors_only=status == "error")
    return {"traces": traces}


@router.get("/{trace_id}")
def get_trace(trace_id: str, request: Request) -> dict:
    # Validate before touching the database: only 32 lowercase hex chars.
    if not _TRACE_ID.match(trace_id):
        raise HTTPException(status_code=400, detail="trace_id must be 32 lowercase hex characters")
    trace = request.app.state.trace_store.get_trace(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return trace
