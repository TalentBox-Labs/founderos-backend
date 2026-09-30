"""Marketing OS — Publishing Engine API (Sprint M1).

Additive orchestration endpoints only. No website/social/campaign implementation.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from revenue_os.services.content_ops_authority import content_ops_human_actor
from runner_api_routers.utils import require_human_or_api_key
from src.tools import publishing_engine as pe

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/publishing", tags=["publishing"])


class CreatePublishJobRequest(BaseModel):
    content_id: str = Field(..., min_length=3, description="Editorial bundle / week id")
    channel: str = Field(..., description="Target channel (website, linkedin, …)")
    requested_by: str = Field(..., min_length=2, description="Human requester name")
    notes: str = Field(default="", description="Optional notes")


class PublishActionRequest(BaseModel):
    requested_by: str = Field(..., min_length=2, description="Human requester name")
    notes: str = Field(default="")


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    jid = str(job.get("job_id") or "")
    return {
        "ok": True,
        "job": job,
        "job_id": jid,
        "bundle_id": job.get("bundle_id"),
        "channel": job.get("channel"),
        "state": job.get("state"),
        "audit": pe.load_audit_for_job(jid),
        "orchestration_only": True,
        "website_engine": False,
        "social_engine": False,
    }


@router.get("/channels", tags=["publishing"])
def get_publishing_channels(
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Registered publish channels (adapter interfaces only)."""
    return {
        "ok": True,
        "channels": pe.list_channels(),
        "states": list(pe.PUBLISH_STATES),
    }


@router.get("/jobs", tags=["publishing"])
def list_publishing_jobs(
    include_terminal: bool = False,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Publish queue — orchestration jobs only."""
    logger.info("Publishing jobs list")
    items = pe.list_queue(include_terminal=include_terminal)
    return {
        "ok": True,
        "count": len(items),
        "items": items,
        "orchestration_only": True,
    }


@router.post("/jobs", tags=["publishing"])
def create_publishing_job(
    body: CreatePublishJobRequest,
    request: Request,
    organization_id: str = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Create a publish job for an editorially approved bundle.

    Tenant scope is the dependency result. Body, query, and header
    organization fields are not read.
    """
    logger.info(
        "Publishing job create",
        extra={"content_id": body.content_id, "channel": body.channel},
    )
    actor = content_ops_human_actor(request, body.requested_by)
    try:
        job = pe.create_publish_job(
            content_id=body.content_id,
            channel=body.channel,
            requested_by=actor,
            tenant_id=organization_id,
            notes=body.notes or "",
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _job_view(job)


@router.get("/{job_id}", tags=["publishing"])
def get_publishing_job(
    job_id: str,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Publish job detail + audit."""
    job = pe.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Publish job not found")
    return _job_view(job)


@router.post("/{job_id}/publish", tags=["publishing"])
def post_publishing_publish(
    job_id: str,
    body: PublishActionRequest,
    request: Request,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Manual publish command (no scheduling / Celery / AI)."""
    logger.info("Publishing manual publish", extra={"job_id": job_id})
    actor = content_ops_human_actor(request, body.requested_by)
    try:
        job = pe.manual_publish(
            job_id,
            requested_by=actor,
            notes=body.notes or "",
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _job_view(job)


@router.post("/{job_id}/retry", tags=["publishing"])
def post_publishing_retry(
    job_id: str,
    body: PublishActionRequest,
    request: Request,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Retry a failed publish job (manual)."""
    logger.info("Publishing retry", extra={"job_id": job_id})
    actor = content_ops_human_actor(request, body.requested_by)
    try:
        job = pe.retry_job(
            job_id,
            requested_by=actor,
            notes=body.notes or "",
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _job_view(job)


@router.post("/{job_id}/cancel", tags=["publishing"])
def post_publishing_cancel(
    job_id: str,
    body: PublishActionRequest,
    request: Request,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Cancel a pending/failed/retry job."""
    logger.info("Publishing cancel", extra={"job_id": job_id})
    actor = content_ops_human_actor(request, body.requested_by)
    try:
        job = pe.cancel_job(
            job_id,
            requested_by=actor,
            notes=body.notes or "",
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _job_view(job)
