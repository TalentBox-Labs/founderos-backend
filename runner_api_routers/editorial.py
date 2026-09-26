"""Editorial Engine readiness + Approval Engine APIs over existing Founder evidence.

E6B readiness remains read-only. E7 adds additive approval endpoints only.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from runner_api_routers.content_studio import build_content_detail, build_content_list
from runner_api_routers.utils import PROJECT_ROOT, require_human_or_api_key
from src.tools import editorial_approval as ea

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/editorial", tags=["editorial"])


class EditorialDecisionRequest(BaseModel):
    """Human decision payload (FDR-002)."""

    approver: str = Field(..., min_length=2, description="Human approver name")
    notes: str = Field(default="", description="Approval / rejection notes")
    phase: str | None = Field(
        default=None, description="Phase-scoped promote target: 2a | 2b | 3"
    )
    staging_root: str | None = Field(
        default=None, description="Optional repo-relative staging dir override"
    )

# Default validator report filenames — same conventions as runner_api_routers/ui.py.
_DEFAULT_REPORTS: tuple[tuple[str, str], ...] = (
    ("research_mapper", "Research_Map.md"),
    ("draft_validator", "Draft_Validation.md"),
    ("structure_checker", "Structure_Check.md"),
    ("metadata_checker", "Metadata_Check.md"),
    ("publish_checklist_checker", "Publish_Checklist_Check.md"),
)

# Optional reports — presence-only; not required for default validation_passed.
_OPTIONAL_REPORTS: tuple[tuple[str, str], ...] = (
    ("crewai_qa", "CrewAI_QA.md"),
    ("content_quality_checker", "Content_Quality_Check.md"),
)


def _qa_reports_dir() -> Path:
    return PROJECT_ROOT / "output" / "qa_reports"


def _parse_report_verdict(text: str) -> str:
    """Extract Final Verdict from a validator report. Does not invent PASS/FAIL."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().lower() == "## final verdict":
            for nxt in lines[i + 1 :]:
                token = nxt.strip()
                if not token:
                    continue
                upper = token.upper()
                if upper == "PASS":
                    return "PASS"
                if upper == "FAIL":
                    return "FAIL"
                return "malformed"
            return "malformed"
    # Fallback: exact last non-empty line PASS/FAIL (legacy short reports).
    for line in reversed(lines):
        token = line.strip().upper()
        if token == "PASS":
            return "PASS"
        if token == "FAIL":
            return "FAIL"
    return "missing_verdict"


def _inspect_report(content_id: str, suffix: str) -> dict[str, Any]:
    """Read one existing QA report file. Never runs validators."""
    path = _qa_reports_dir() / f"{content_id}_{suffix}"
    rel = f"output/qa_reports/{content_id}_{suffix}"
    if not path.is_file():
        return {
            "available": False,
            "path": rel,
            "verdict": "missing",
        }
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "available": True,
            "path": rel,
            "verdict": "unreadable",
            "error": str(exc),
        }
    if not text.strip():
        return {
            "available": True,
            "path": rel,
            "verdict": "malformed",
            "error": "empty report",
        }
    return {
        "available": True,
        "path": rel,
        "verdict": _parse_report_verdict(text),
    }


def _bool_from_verdict(verdict: str) -> bool | None:
    """Map report verdict to bool; None = incomplete / non-boolean evidence."""
    if verdict == "PASS":
        return True
    if verdict == "FAIL":
        return False
    return None


def _readiness_summary(
    *,
    draft_available: bool,
    final_artifact_available: bool,
    reports: dict[str, dict[str, Any]],
) -> str:
    """
    Observational summary only — not approval or publish permission.

    Values are evidence descriptors, not lifecycle stages.
    """
    default_verdicts = [reports[name]["verdict"] for name, _ in _DEFAULT_REPORTS]
    if any(v == "FAIL" for v in default_verdicts):
        return "has_failures"
    if any(v != "PASS" for v in default_verdicts):
        return "incomplete_evidence"
    if not draft_available and not final_artifact_available:
        return "incomplete_evidence"
    return "all_default_reports_pass"


def build_editorial_readiness(content_id: str) -> dict[str, Any]:
    """Build readiness payload from tracker + artifacts + existing QA reports."""
    detail = build_content_detail(content_id)
    item = detail["item"]
    artifacts = dict(item.get("artifacts") or {})

    draft_available = bool(artifacts.get("draft"))
    final_artifact_available = bool(artifacts.get("final"))
    publish_checklist_available = bool(artifacts.get("checklist"))

    reports: dict[str, dict[str, Any]] = {}
    for name, suffix in _DEFAULT_REPORTS + _OPTIONAL_REPORTS:
        reports[name] = _inspect_report(content_id, suffix)

    draft_report = reports["draft_validator"]
    structure_report = reports["structure_checker"]
    metadata_report = reports["metadata_checker"]

    # qa_available: existing Founder QA evidence (draft validation report and/or tracker qa_status).
    qa_status = str(item.get("qa_status") or "").strip()
    qa_available = bool(draft_report["available"] or qa_status or reports["crewai_qa"]["available"])

    default_verdicts = [reports[name]["verdict"] for name, _ in _DEFAULT_REPORTS]
    if any(v == "FAIL" for v in default_verdicts):
        validation_passed: bool | None = False
    elif all(v == "PASS" for v in default_verdicts):
        validation_passed = True
    else:
        validation_passed = None

    summary = _readiness_summary(
        draft_available=draft_available,
        final_artifact_available=final_artifact_available,
        reports=reports,
    )

    return {
        "ok": True,
        "content_id": content_id,
        "content_studio_path": f"/content-studio/{content_id}",
        "tracker": {
            "status": item.get("status") or "",
            "qa_status": item.get("qa_status") or "",
            "current_step": item.get("current_step") or "",
            "next_step": item.get("next_step") or "",
            "title": item.get("title") or "",
            "draft_path": item.get("draft_path") or "",
            "final_output_path": item.get("final_output_path") or "",
            "qa_output_path": item.get("qa_output_path") or "",
        },
        "artifacts": artifacts,
        "reports": reports,
        "readiness": {
            "draft_available": draft_available,
            "editor_review_available": final_artifact_available,
            "qa_available": qa_available,
            "validation_passed": validation_passed,
            "metadata_valid": _bool_from_verdict(metadata_report["verdict"]),
            "structure_valid": _bool_from_verdict(structure_report["verdict"]),
            "final_artifact_available": final_artifact_available,
            "publish_checklist_available": publish_checklist_available,
            "readiness_summary": summary,
        },
    }


@router.get("/readiness/{content_id}", tags=["editorial"])
def get_editorial_readiness(
    content_id: str,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Return observed editorial readiness evidence (read-only; no mutation)."""
    logger.info("Editorial readiness", extra={"content_id": content_id})
    try:
        return build_editorial_readiness(content_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — explicit API failure, no fake readiness
        logger.exception("Editorial readiness failed", extra={"content_id": content_id})
        raise HTTPException(status_code=500, detail=str(exc) or "Readiness failure") from exc


# ---------------------------------------------------------------------------
# E7 — Editorial Approval Engine (additive; does not mutate readiness contract)
# ---------------------------------------------------------------------------


def build_editorial_item(content_id: str) -> dict[str, Any]:
    """Compose approval view from readiness + decision audit (existing artifacts only)."""
    readiness = build_editorial_readiness(content_id)
    item = build_editorial_item_from_readiness(readiness)
    return {"ok": True, **item}


def build_editorial_item_from_readiness(readiness: dict[str, Any]) -> dict[str, Any]:
    content_id = str(readiness.get("content_id") or "")
    tracker = dict(readiness.get("tracker") or {})
    return ea.build_item_view(
        content_id=content_id,
        title=str(tracker.get("title") or ""),
        artifacts=dict(readiness.get("artifacts") or {}),
        readiness=dict(readiness.get("readiness") or {}),
        tracker=tracker,
    )


def build_editorial_pending() -> dict[str, Any]:
    """Pending queue: content not successfully Approved for suggested phase."""
    listing = build_content_list()
    pending: list[dict[str, Any]] = []
    for raw in listing.get("items") or []:
        cid = str(raw.get("content_id") or "").strip()
        if not cid:
            continue
        try:
            readiness = build_editorial_readiness(cid)
            view = build_editorial_item_from_readiness(readiness)
        except HTTPException:
            continue
        if view.get("editorial_state") in ea.PENDING_STATES:
            pending.append(
                {
                    "content_id": view["content_id"],
                    "title": view.get("title") or "",
                    "editorial_state": view["editorial_state"],
                    "phase": view["phase"],
                    "bundle": view["bundle"],
                    "staging_available": view["staging_available"],
                    "can_approve": view["can_approve"],
                    "latest_decision": view.get("latest_decision"),
                    "detail_path": f"/api/v1/editorial/{view['content_id']}",
                    "ui_path": f"/editorial/{view['content_id']}",
                }
            )
    return {
        "ok": True,
        "count": len(pending),
        "items": pending,
        "authorizes_publish": False,
        "states": list(ea.EDITORIAL_STATES),
    }


def _run_decision(
    content_id: str,
    decision: str,
    body: EditorialDecisionRequest,
) -> dict[str, Any]:
    readiness = build_editorial_readiness(content_id)
    tracker = dict(readiness.get("tracker") or {})
    try:
        result = ea.apply_decision(
            content_id=content_id,
            decision=decision,
            approver=body.approver,
            notes=body.notes or "",
            phase=body.phase,
            staging_root=body.staging_root,
            artifacts=dict(readiness.get("artifacts") or {}),
            readiness_summary=(readiness.get("readiness") or {}).get("readiness_summary"),
            validation_passed=(readiness.get("readiness") or {}).get("validation_passed"),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    record = result["record"]
    # Recompute view after append.
    view = build_editorial_item_from_readiness(build_editorial_readiness(content_id))
    return {
        "ok": result["ok"],
        "content_id": content_id.upper(),
        "decision": decision,
        "editorial_state": view["editorial_state"],
        "phase": record.get("phase"),
        "bundle": record.get("bundle"),
        "approver": record.get("approver"),
        "utc_timestamp": record.get("utc_timestamp"),
        "notes": record.get("notes"),
        "authorizes_publish": False,
        "promotion": record.get("promotion"),
        "audit_record": record,
        "item": view,
        "title": tracker.get("title") or "",
    }


@router.get("/pending", tags=["editorial"])
def get_editorial_pending(
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Pending Editorial Approval queue (additive; read + decision state only)."""
    logger.info("Editorial pending queue")
    try:
        return build_editorial_pending()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Editorial pending failed")
        raise HTTPException(status_code=500, detail=str(exc) or "Pending failure") from exc


@router.get("/{content_id}", tags=["editorial"])
def get_editorial_item(
    content_id: str,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Editorial Approval detail for one content bundle."""
    logger.info("Editorial item", extra={"content_id": content_id})
    try:
        return build_editorial_item(content_id)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Editorial item failed", extra={"content_id": content_id})
        raise HTTPException(status_code=500, detail=str(exc) or "Editorial item failure") from exc


@router.post("/{content_id}/approve", tags=["editorial"])
def post_editorial_approve(
    content_id: str,
    body: EditorialDecisionRequest,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Human approve → phase-scoped promote only (FDR-001/002). Does not publish (FDR-003)."""
    logger.info("Editorial approve", extra={"content_id": content_id})
    return _run_decision(content_id, ea.DECISION_APPROVE, body)


@router.post("/{content_id}/reject", tags=["editorial"])
def post_editorial_reject(
    content_id: str,
    body: EditorialDecisionRequest,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Human reject — audit only; no promote; no publish."""
    logger.info("Editorial reject", extra={"content_id": content_id})
    return _run_decision(content_id, ea.DECISION_REJECT, body)


@router.post("/{content_id}/request-changes", tags=["editorial"])
def post_editorial_request_changes(
    content_id: str,
    body: EditorialDecisionRequest,
    _: str | None = Depends(require_human_or_api_key),
) -> dict[str, Any]:
    """Human request-changes — audit only; no promote; no publish."""
    logger.info("Editorial request-changes", extra={"content_id": content_id})
    return _run_decision(content_id, ea.DECISION_REQUEST_CHANGES, body)
