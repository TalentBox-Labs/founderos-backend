"""Marketing campaign and content generation endpoints."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from revenue_os.config import settings

from runner_api_routers.utils import _run, _tail, require_human_or_api_key, PROJECT_ROOT

logger = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["marketing"])


class MarketingRequest(BaseModel):
    """Marketing campaign request parameters."""

    brand: str = "workcrew"
    topic: str = ""
    keyword: str = ""
    geo: str = ""
    funnel: str = "consideration"
    output_root: str | None = None
    status_path: str = ""
    instagram_image_url: str = ""
    confirmed: bool = False
    channel: str = "all"


def _social_publisher():
    """Lazy load social publisher (optional dependency)."""
    from revenue_os.integrations.social_publisher import SocialPublisher

    return SocialPublisher()


def _marketing_integration_status() -> dict[str, bool]:
    """Check which marketing integrations are configured."""
    return {
        "hashnode": bool(os.environ.get("HASHNODE_ACCESS_TOKEN")),
        "linkedin": bool(os.environ.get("LINKEDIN_ACCESS_TOKEN")),
        "instagram": bool(os.environ.get("INSTAGRAM_ACCESS_TOKEN")),
        "youtube": bool(os.environ.get("YOUTUBE_API_KEY")),
    }


def _marketing_runs() -> list[dict[str, Any]]:
    """Scan output/marketing for publish status manifests."""
    mkt_dir = PROJECT_ROOT / "output" / "marketing"
    runs = []
    if not mkt_dir.is_dir():
        return runs
    for status_file in sorted(mkt_dir.rglob("08_Publish_Status.json"), reverse=True):
        try:
            data = json.loads(status_file.read_text(encoding="utf-8"))
            data["slug"] = status_file.parent.name
            runs.append(data)
        except Exception as e:
            logger.warning(f"Failed to load {status_file}: {e}")
            pass
    return runs[:20]


@router.post("/marketing/generate", tags=["marketing"])
def marketing_generate(
    req: MarketingRequest,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Generate multi-channel marketing content via CrewAI agents."""
    if not req.topic or not req.keyword:
        raise HTTPException(status_code=400, detail="topic and keyword are required")

    logger.info(
        "Generating marketing content",
        extra={
            "brand": req.brand,
            "topic": req.topic,
            "keyword": req.keyword,
            "channel": req.channel,
        },
    )

    output_root = req.output_root or str(PROJECT_ROOT / "output" / "marketing")
    output_path = Path(output_root) / req.topic.replace(" ", "_").lower()
    output_path.mkdir(parents=True, exist_ok=True)

    # Live path must invoke the existing CLI module (src.marketing_crew).
    # revenue_os.agents.marketing_crew does not exist (Sprint C blocker).
    r = _run(
        [
            sys.executable,
            "-m",
            "src.marketing_crew",
            "--brand",
            req.brand,
            "--topic",
            req.topic,
            "--keyword",
            req.keyword,
            "--geo",
            req.geo,
            "--funnel",
            req.funnel,
            "--output",
            str(output_path),
        ]
    )

    return {
        "ok": r.returncode == 0,
        "stdout": _tail(r.stdout or ""),
        "stderr": _tail(r.stderr or ""),
        "output_path": str(output_path),
    }


@router.post("/marketing/dry-run", tags=["marketing"])
def marketing_dry_run(
    req: MarketingRequest,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Preview what publish_all would post without hitting any APIs."""
    if not req.status_path:
        raise HTTPException(status_code=400, detail="status_path is required")

    logger.info(
        "Running marketing dry-run",
        extra={"status_path": req.status_path, "brand": req.brand},
    )

    try:
        pub = _social_publisher()
        return pub.dry_run(req.status_path)
    except Exception as e:
        logger.error(f"Marketing dry-run failed: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}


@router.post("/marketing/publish", tags=["marketing"])
def marketing_publish(
    req: MarketingRequest,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Publish to all configured social channels. Requires confirmed=True."""
    if not req.status_path:
        raise HTTPException(status_code=400, detail="status_path is required")
    if not req.confirmed:
        raise HTTPException(status_code=400, detail="confirmed must be true to publish")

    logger.info(
        "Publishing marketing content",
        extra={"status_path": req.status_path, "brand": req.brand},
    )

    try:
        pub = _social_publisher()
        return pub.publish_all(
            req.status_path,
            instagram_image_url=req.instagram_image_url,
            confirmed=True,
        )
    except Exception as e:
        logger.error(f"Marketing publish failed: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}
