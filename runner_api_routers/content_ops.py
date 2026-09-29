"""Content Ops current-week read.

Authenticated HUMAN, bound to the explicit single-organization beta lock.
Read-only. Query, header, cookie, and body fields do not select the week
or the tenant and do not supply allowed actions.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from runner_api_routers.utils import require_human_or_api_key
from src.tools.content_ops_current_week import read_current_week

router = APIRouter(prefix="/api/v1/content-ops", tags=["content-ops"])


@router.get("/weeks/current")
def get_current_content_ops_week(
    request: Request,
    _organization_id: str = Depends(require_human_or_api_key),
) -> JSONResponse:
    """Return the server-selected current week. Does not mutate."""
    del request
    payload = read_current_week()
    status = int(payload["http_status"])
    return JSONResponse(status_code=status, content=payload)
