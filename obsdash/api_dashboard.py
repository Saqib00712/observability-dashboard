"""Phase 8: dashboard read API -- overview numbers and the error list.
Both are computed from spans in the trace store (see analytics.py)."""

import time

from fastapi import APIRouter, Depends, Query, Request

from obsdash.analytics import build_errors, build_overview
from obsdash.auth import require_token

router = APIRouter(prefix="/api", tags=["dashboard"], dependencies=[Depends(require_token)])

_WINDOW = Query(60, ge=1, le=10_080, description="Look-back window in minutes (max 7 days).")


@router.get("/overview")
def overview(request: Request, window_minutes: int = _WINDOW) -> dict:
    store = request.app.state.trace_store
    now_ns = time.time_ns()
    rows = store.query_spans(since_ns=now_ns - window_minutes * 60 * 1_000_000_000)
    return build_overview(rows, window_minutes, now_ns, store.bounds())


@router.get("/errors")
def errors(request: Request, window_minutes: int = _WINDOW, limit: int = Query(50, ge=1, le=200)) -> dict:
    store = request.app.state.trace_store
    now_ns = time.time_ns()
    rows = store.query_spans(
        since_ns=now_ns - window_minutes * 60 * 1_000_000_000,
        kinds=("SERVER", "CLIENT", "INTERNAL", "PRODUCER", "CONSUMER"),
    )
    return build_errors(rows, window_minutes, now_ns, limit)
