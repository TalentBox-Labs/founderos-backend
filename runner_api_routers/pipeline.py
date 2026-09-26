"""Pipeline execution and validation endpoints."""

from __future__ import annotations

import logging
import sys
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from runner_api_routers.utils import (
    _apply_week_if_set,
    _run,
    _tail,
    require_human_or_api_key,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["pipeline"])


class WeekRequest(BaseModel):
    """Request model for week-based pipeline operations."""

    week: str | None = None
    topic: str | None = None
    url: str | None = None


@router.post("/run", tags=["pipeline"])
def run_full(
    request: WeekRequest,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Full pipeline: optional week switch then main.py."""
    logger.info("Starting full pipeline run", extra={"week": request.week})
    week = (request.week or "").strip().upper() or None
    ok, steps = _apply_week_if_set(week)
    if not ok:
        logger.error("Week switch failed", extra={"week": week})
        return {"ok": False, "week": week, "steps": steps, "stderr": steps[-1]["stderr"]}
    r = _run([sys.executable, "-m", "src.tools.pipeline_orchestrator"])
    return {
        "ok": r.returncode == 0,
        "week": week,
        "stdout": _tail(r.stdout or ""),
        "stderr": _tail(r.stderr or ""),
        "steps": steps,
    }


@router.post("/validate", tags=["pipeline"])
def run_validate(
    request: WeekRequest,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Run validators only (no generation, no tracker write)."""
    logger.info("Running validators", extra={"week": request.week})
    week = (request.week or "").strip().upper() or None
    ok, steps = _apply_week_if_set(week)
    if not ok:
        logger.error("Week switch failed", extra={"week": week})
        return {"ok": False, "steps": steps}
    r = _run([sys.executable, "-m", "src.tools.pipeline_runner"])
    return {
        "ok": r.returncode == 0,
        "stdout": _tail(r.stdout or ""),
        "stderr": _tail(r.stderr or ""),
    }


@router.post("/generate", tags=["pipeline"])
def run_generate(
    request: WeekRequest,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Phase 2A: content generation crew."""
    logger.info("Running generation crew", extra={"week": request.week})
    week = (request.week or "").strip().upper() or None
    ok, steps = _apply_week_if_set(week)
    if not ok:
        logger.error("Week switch failed", extra={"week": week})
        return {"ok": False, "steps": steps}
    r = _run([sys.executable, "-m", "src.generation_crew"])
    return {
        "ok": r.returncode == 0,
        "stdout": _tail(r.stdout or ""),
        "stderr": _tail(r.stderr or ""),
    }


@router.post("/edit", tags=["pipeline"])
def run_edit(
    request: WeekRequest,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Phase 2B: editor crew."""
    logger.info("Running editor crew", extra={"week": request.week})
    week = (request.week or "").strip().upper() or None
    ok, steps = _apply_week_if_set(week)
    if not ok:
        logger.error("Week switch failed", extra={"week": week})
        return {"ok": False, "steps": steps}
    r = _run([sys.executable, "-m", "src.editor_crew"])
    return {
        "ok": r.returncode == 0,
        "stdout": _tail(r.stdout or ""),
        "stderr": _tail(r.stderr or ""),
    }


@router.post("/switch-week", tags=["pipeline"])
def switch_week(
    request: WeekRequest,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Apply a week profile to runtime_config.json."""
    week = (request.week or "").strip().upper()
    if not week:
        raise HTTPException(status_code=400, detail="week is required")
    logger.info("Switching week", extra={"week": week})
    r = _run([sys.executable, "-m", "src.tools.runtime_apply", week])
    return {"ok": r.returncode == 0, "week": week, "stdout": r.stdout, "stderr": r.stderr}


@router.post("/go-live", tags=["pipeline"])
def record_go_live(
    request: WeekRequest,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Record that a week's article is live at a URL."""
    week = (request.week or "").strip().upper()
    url = (request.url or "").strip()
    if not week or not url:
        raise HTTPException(status_code=400, detail="week and url are required")
    logger.info("Recording go-live", extra={"week": week, "url": url})
    ok, steps = _apply_week_if_set(week)
    if not ok:
        logger.error("Week switch failed", extra={"week": week})
        return {"ok": False, "steps": steps}
    r = _run(
        [
            sys.executable,
            "-m",
            "src.tools.go_live_helpers",
            "record-live",
            "--url",
            url,
            "--i-confirmed-url-live",
        ]
    )
    return {
        "ok": r.returncode == 0,
        "stdout": _tail(r.stdout or ""),
        "stderr": _tail(r.stderr or ""),
    }


@router.post("/run-pipeline", tags=["pipeline"])
def run_pipeline(
    request: WeekRequest,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """
    Optionally apply a week profile, then run the same entrypoint as ``python main.py``.

    Legacy alias for compatibility.
    """
    logger.info("Running legacy pipeline", extra={"week": request.week})
    steps: list[dict] = []
    week = (request.week or "").strip().upper() or None

    if week:
        r = _run([sys.executable, "-m", "src.tools.runtime_apply", week])
        steps.append(
            {
                "step": "runtime_apply",
                "returncode": r.returncode,
                "stdout_tail": _tail(r.stdout or ""),
                "stderr_tail": _tail(r.stderr or ""),
            }
        )
        if r.returncode != 0:
            logger.error("Week switch failed", extra={"week": week})
            return {
                "status": "failed",
                "week": week,
                "steps": steps,
                "stderr": _tail(r.stderr or ""),
            }

    r = _run([sys.executable, "-m", "src.tools.pipeline_orchestrator"])
    steps.append(
        {
            "step": "pipeline_orchestrator",
            "returncode": r.returncode,
            "stdout_tail": _tail(r.stdout or ""),
            "stderr_tail": _tail(r.stderr or ""),
        }
    )

    logger.info(
        "Pipeline execution complete",
        extra={"week": week, "returncode": r.returncode},
    )
    # Canonical status values for this legacy endpoint: "ok" | "failed".
    # (Newer pipeline routes use boolean ``ok``; do not use "success" here.)
    return {
        "status": "ok" if r.returncode == 0 else "failed",
        "week": week,
        "steps": steps,
        "stdout": _tail(r.stdout or ""),
        "stderr": _tail(r.stderr or ""),
    }
