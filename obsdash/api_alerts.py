"""Phase 9: read API for alert state and history."""

from fastapi import APIRouter, Depends, Query, Request
from obsdash.auth import require_token

router = APIRouter(prefix="/api", tags=["alerts"], dependencies=[Depends(require_token)])


@router.get("/alerts")
def alerts(request: Request, event_limit: int = Query(50, ge=1, le=500)) -> dict:
    return request.app.state.alerts.snapshot(event_limit)
