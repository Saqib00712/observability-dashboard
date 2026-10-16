"""Phase 7: cost/token summary API (Phase 8's cost page reads this)."""

from fastapi import APIRouter, Depends, Query, Request
from obsdash.auth import require_token

router = APIRouter(prefix="/api/costs", tags=["costs"], dependencies=[Depends(require_token)])


@router.get("/summary")
def cost_summary(request: Request, window_minutes: int | None = Query(None, ge=1, le=525_600)) -> dict:
    return request.app.state.trace_store.cost_summary(window_minutes)
