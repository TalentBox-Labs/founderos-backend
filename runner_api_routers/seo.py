"""SEO/GEO keyword tracking — manual rank + AI-answer-engine visibility log.

No live rank-checking integration (no search-console/SERP credentials in
this environment) — the founder logs what they observe, and this turns that
into a trend per keyword plus an aggregate score for the Dashboard.
"""

from __future__ import annotations

import logging
import uuid as uuid_lib
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from revenue_os.database import SessionLocal
from revenue_os.models.seo import SEOKeyword, SEORankCheck
from runner_api_routers.utils import require_human_or_api_key
from src.tools.seo_engine import (
    analyze_page_artifact,
    analyze_site,
    analyze_technical_page,
    analyze_technical_site,
    default_artifact_root,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/seo", tags=["seo"])


def _check_dict(c: SEORankCheck) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "rank": c.rank,
        "ai_visible": bool(c.ai_visible),
        "source": c.source,
        "notes": c.notes,
        "checked_at": c.checked_at.isoformat() if c.checked_at else None,
    }


def _keyword_dict(k: SEOKeyword, checks: list[SEORankCheck] | None = None) -> dict[str, Any]:
    history = list(k.checks) if checks is None else checks
    latest = history[-1] if history else None
    previous = history[-2] if len(history) > 1 else None
    trend = None
    if latest and previous and latest.rank is not None and previous.rank is not None:
        trend = previous.rank - latest.rank  # positive = improved (moved up the results)

    d = {
        "id": str(k.id),
        "keyword": k.keyword,
        "target_url": k.target_url,
        "geo": k.geo,
        "target_rank": k.target_rank,
        "notes": k.notes,
        "created_at": k.created_at.isoformat() if k.created_at else None,
        "current_rank": latest.rank if latest else None,
        "ai_visible": bool(latest.ai_visible) if latest else False,
        "last_checked_at": latest.checked_at.isoformat() if latest and latest.checked_at else None,
        "trend": trend,
        "checks_count": len(history),
    }
    return d


class KeywordCreateRequest(BaseModel):
    keyword: str = Field(..., min_length=1, max_length=255)
    target_url: str | None = Field(default=None, max_length=500)
    geo: str | None = Field(default=None, max_length=100)
    target_rank: int | None = Field(default=None, ge=1)
    notes: str | None = None


class RankCheckRequest(BaseModel):
    rank: int | None = Field(default=None, ge=1)
    ai_visible: bool = False
    source: str = Field(default="manual", max_length=50)
    notes: str | None = None


@router.get("/keywords")
def list_keywords(_: str | None = Depends(require_human_or_api_key)) -> dict[str, Any]:
    db = SessionLocal()
    try:
        keywords = db.query(SEOKeyword).order_by(SEOKeyword.created_at.desc()).all()
        return {"ok": True, "count": len(keywords), "keywords": [_keyword_dict(k) for k in keywords]}
    finally:
        db.close()


@router.post("/keywords")
def create_keyword(
    req: KeywordCreateRequest,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        keyword = SEOKeyword(
            keyword=req.keyword, target_url=req.target_url, geo=req.geo,
            target_rank=req.target_rank, notes=req.notes,
        )
        db.add(keyword)
        db.commit()
        db.refresh(keyword)
        return {"ok": True, "keyword": _keyword_dict(keyword, checks=[])}
    finally:
        db.close()


@router.get("/keywords/{keyword_id}")
def get_keyword(
    keyword_id: str,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        try:
            kid = uuid_lib.UUID(keyword_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid keyword_id")
        keyword = db.get(SEOKeyword, kid)
        if keyword is None:
            raise HTTPException(status_code=404, detail="Keyword not found")
        checks = (
            db.query(SEORankCheck)
            .filter(SEORankCheck.keyword_id == kid)
            .order_by(SEORankCheck.checked_at)
            .all()
        )
        result = _keyword_dict(keyword, checks=checks)
        result["history"] = [_check_dict(c) for c in checks]
        return {"ok": True, "keyword": result}
    finally:
        db.close()


@router.delete("/keywords/{keyword_id}")
def delete_keyword(
    keyword_id: str,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        try:
            kid = uuid_lib.UUID(keyword_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid keyword_id")
        keyword = db.get(SEOKeyword, kid)
        if keyword is None:
            raise HTTPException(status_code=404, detail="Keyword not found")
        db.delete(keyword)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@router.post("/keywords/{keyword_id}/checks")
def log_rank_check(
    keyword_id: str,
    req: RankCheckRequest,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        try:
            kid = uuid_lib.UUID(keyword_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid keyword_id")
        keyword = db.get(SEOKeyword, kid)
        if keyword is None:
            raise HTTPException(status_code=404, detail="Keyword not found")

        check = SEORankCheck(
            keyword_id=kid, rank=req.rank, ai_visible=req.ai_visible,
            source=req.source, notes=req.notes,
        )
        db.add(check)
        db.commit()
        db.refresh(check)
        return {"ok": True, "check": _check_dict(check)}
    finally:
        db.close()


@router.get("/summary")
def seo_summary(_: str | None = Depends(require_human_or_api_key)) -> dict[str, Any]:
    """Aggregate score for the Dashboard — tracked keywords, rank trend, AI visibility."""
    db = SessionLocal()
    try:
        keywords = db.query(SEOKeyword).all()
        views = [_keyword_dict(k) for k in keywords]
    finally:
        db.close()

    ranked = [v for v in views if v["current_rank"] is not None]
    improved = [v for v in views if v.get("trend") is not None and v["trend"] > 0]
    declined = [v for v in views if v.get("trend") is not None and v["trend"] < 0]

    return {
        "ok": True,
        "keywords_tracked": len(views),
        "keywords_ranked": len(ranked),
        "avg_rank": round(sum(v["current_rank"] for v in ranked) / len(ranked), 1) if ranked else None,
        "ai_visible_count": sum(1 for v in views if v["ai_visible"]),
        "improved_count": len(improved),
        "declined_count": len(declined),
    }


# ---------------------------------------------------------------------------
# S1 — SEO Readiness Engine (read-only; additive; no mutation / indexing)
# ---------------------------------------------------------------------------


@router.get("/readiness")
def seo_readiness_list(_: str | None = Depends(require_human_or_api_key)) -> dict[str, Any]:
    """Read-only site SEO readiness over Website Engine artifacts."""
    result = analyze_site()
    payload = result.to_dict()
    payload["ok"] = True
    payload["production_seo_activation"] = "BLOCKED"
    return payload


@router.get("/readiness/{slug}")
def seo_readiness_page(
    slug: str,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Read-only page SEO readiness for a single artifact slug."""
    result = analyze_page_artifact(default_artifact_root(), slug)
    if any(c.id == "html_missing" for c in result.checks):
        raise HTTPException(status_code=404, detail=f"Page artifact not found: {slug}")
    payload = result.to_dict()
    payload["ok"] = True
    payload["production_seo_activation"] = "BLOCKED"
    return payload


# ---------------------------------------------------------------------------
# S2 — Technical SEO Engine (additive; S1 readiness contract unchanged)
# ---------------------------------------------------------------------------


@router.get("/technical")
@router.get("/technical/site")
def seo_technical_site(_: str | None = Depends(require_human_or_api_key)) -> dict[str, Any]:
    """Read-only site technical SEO analysis over Website Engine artifacts."""
    result = analyze_technical_site()
    payload = result.to_dict()
    payload["ok"] = True
    return payload


@router.get("/technical/{slug}")
def seo_technical_page(
    slug: str,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Read-only page technical SEO analysis."""
    if slug in {"site", "readiness"}:
        raise HTTPException(status_code=404, detail=f"Page artifact not found: {slug}")
    result = analyze_technical_page(default_artifact_root(), slug)
    if any(f.id == "tech_html_missing" for f in result.findings):
        raise HTTPException(status_code=404, detail=f"Page artifact not found: {slug}")
    payload = result.to_dict()
    payload["ok"] = True
    payload["production_seo_activation"] = "BLOCKED"
    return payload
