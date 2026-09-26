"""Content Studio read-only JSON API over Founder tracker.csv + input/."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from runner_api_routers.utils import (
    _read_tracker,
    _validate_week_id,
    require_human_or_api_key,
    _week_artifacts,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/content-studio", tags=["content-studio"])

# Tracker.csv columns only (no invented fields).
_TRACKER_FIELDS = (
    "content_id",
    "title",
    "status",
    "qa_status",
    "current_step",
    "next_step",
    "draft_path",
    "qa_output_path",
    "final_output_path",
    "artifact_folder",
)

# Artifact keys from _week_artifacts → conventional input/ filenames (utils.py).
ARTIFACT_FILES: dict[str, str] = {
    "brief": "01_Content_Brief.md",
    "seo": "02_SEO_Plan.md",
    "research": "03_Research.md",
    "draft": "04_Draft.md",
    "final": "05_Final.md",
    "design": "06_Design_Brief.md",
    "social": "07_Social_Posts.md",
    "email": "08_Email_Copy.md",
    "checklist": "09_Publish_Checklist.md",
}


def _artifact_lookup_id(row: dict[str, str]) -> str:
    """Resolve input/ folder for artifact presence (tracker evidence only)."""
    folder = (row.get("artifact_folder") or "").strip()
    if folder:
        return folder
    return (row.get("content_id") or "").strip()


def _serialize_item(row: dict[str, str]) -> dict[str, Any]:
    """Project a tracker row + derived artifact presence flags."""
    item: dict[str, Any] = {field: (row.get(field) or "") for field in _TRACKER_FIELDS}
    lookup = _artifact_lookup_id(row)
    item["artifacts"] = _week_artifacts(lookup) if lookup else {}
    return item


def _require_content_id(content_id: str) -> str:
    """Validate path-safe content id; raise HTTP 400 on bad format."""
    try:
        _validate_week_id(content_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return content_id


def build_content_list() -> dict[str, Any]:
    """Build list payload (shared by HTTP API and UI). Read-only."""
    rows = _read_tracker()
    items: list[dict[str, Any]] = []
    for row in rows:
        cid = (row.get("content_id") or "").strip()
        if not cid:
            continue
        items.append(_serialize_item(row))
    return {"ok": True, "count": len(items), "items": items}


def build_content_detail(content_id: str) -> dict[str, Any]:
    """Build detail payload (shared by HTTP API and UI). Read-only."""
    content_id = _require_content_id(content_id)
    rows = _read_tracker()
    row = next(
        (r for r in rows if (r.get("content_id") or "").strip() == content_id),
        None,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Content not found")
    return {"ok": True, "item": _serialize_item(row)}


@router.get("/content", tags=["content-studio"])
def list_content(
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """List Content Studio inventory from tracker.csv (read-only)."""
    logger.info("Content Studio list")
    return build_content_list()


@router.get("/content/{content_id}", tags=["content-studio"])
def get_content(
    content_id: str,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Return one Content Studio item from tracker.csv (read-only)."""
    logger.info("Content Studio detail", extra={"content_id": content_id})
    return build_content_detail(content_id)
