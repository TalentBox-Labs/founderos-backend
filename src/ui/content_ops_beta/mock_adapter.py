"""Isolated mock adapter for the Content Ops Beta read contract.

This module does not read the content tracker, page metadata, local files, or
any publisher. Every payload is labeled source=mock and live=false. Replace
`get_content_ops_reader` when GET /api/v1/content-ops/weeks/current exists.
"""

from __future__ import annotations

import re
from typing import Any

from src.ui.content_ops_beta.contract import TARGET_READ_CONTRACT, empty_projection

_SCENARIO_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_SAMPLE_URL = "https://example.invalid/content/w12"

SCENARIO_CATALOG: tuple[dict[str, str], ...] = (
    {"id": "empty", "label": "No active week"},
    {"id": "research_drafting", "label": "Research / drafting"},
    {"id": "qa_pass", "label": "QA pass"},
    {"id": "human_review", "label": "Human review required"},
    {"id": "approved_publish_pending", "label": "Approved / publish pending"},
    {"id": "publication_unproven", "label": "Publication UNPROVEN"},
    {"id": "verification_pending", "label": "Verification pending"},
    {"id": "verified", "label": "VERIFIED"},
    {"id": "failed", "label": "Failed"},
    {"id": "unknown_remote", "label": "UNKNOWN_REMOTE"},
    {"id": "http_401", "label": "401"},
    {"id": "http_403", "label": "403"},
    {"id": "http_409", "label": "409 active intent"},
    {"id": "http_422", "label": "422 policy failure"},
    {"id": "loading", "label": "Loading"},
)

_CATALOG_IDS = {item["id"] for item in SCENARIO_CATALOG}


def _envelope(
    scenario: str,
    *,
    http_status: int,
    ok: bool,
    state: str,
    projection: dict[str, Any] | None,
    error: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "source": "mock",
        "live": False,
        "contract": TARGET_READ_CONTRACT,
        "scenario": scenario,
        "http_status": http_status,
        "ok": ok,
        "state": state,
        "projection": projection,
        "error": error,
        "available_scenarios": [dict(item) for item in SCENARIO_CATALOG],
    }


def _ready(scenario: str, **fields: Any) -> dict[str, Any]:
    projection = empty_projection()
    projection.update(fields)
    return _envelope(
        scenario,
        http_status=200,
        ok=True,
        state="ready",
        projection=projection,
        error=None,
    )


def _error(
    scenario: str,
    status: int,
    code: str,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    error = {"code": code, "message": message, **extra}
    return _envelope(
        scenario,
        http_status=status,
        ok=False,
        state="error",
        projection=None,
        error=error,
    )


def _artifacts(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"id": item_id, "label": label} for item_id, label in pairs]


class MockContentOpsReadAdapter:
    """In-memory scenarios. Not a live Content Ops backend."""

    def read_current_week(self, scenario: str | None = None) -> dict[str, Any]:
        selected = (scenario or "").strip() or "empty"
        if not _SCENARIO_RE.fullmatch(selected) or selected not in _CATALOG_IDS:
            return _error(
                "unknown_scenario",
                400,
                "UNKNOWN_SCENARIO",
                "That mock scenario is not defined. No week was loaded.",
            )
        builder = _SCENARIOS[selected]
        return builder()


def _empty() -> dict[str, Any]:
    return _envelope(
        "empty",
        http_status=200,
        ok=True,
        state="empty",
        projection=empty_projection(),
        error=None,
    )


def _loading() -> dict[str, Any]:
    return _envelope(
        "loading",
        http_status=200,
        ok=True,
        state="loading",
        projection=None,
        error=None,
    )


def _research_drafting() -> dict[str, Any]:
    return _ready(
        "research_drafting",
        week_id="W12",
        stage="drafting",
        next_action="Continue research and draft",
        qa_status="NOT_RUN",
        risk_class="LOW",
        approval_required=False,
        publication_status="NOT_PUBLISHED",
        publication_truth="NOT_PUBLISHED",
        verification_status="NOT_APPLICABLE",
        last_run_at="2026-09-28T09:00:00Z",
        next_schedule_at="2026-09-30T09:00:00Z",
        failure=None,
        artifact_refs=_artifacts(("w12-brief", "Week brief")),
        published_url=None,
        allowed_actions=["run_now", "pause_week", "open_artifact", "view_audit"],
    )


def _qa_pass() -> dict[str, Any]:
    return _ready(
        "qa_pass",
        week_id="W12",
        stage="qa",
        next_action="QA passed. Publication has not started.",
        qa_status="PASS",
        risk_class="LOW",
        approval_required=False,
        publication_status="NOT_PUBLISHED",
        publication_truth="NOT_PUBLISHED",
        verification_status="NOT_APPLICABLE",
        last_run_at="2026-09-28T11:00:00Z",
        next_schedule_at="2026-09-30T09:00:00Z",
        failure=None,
        artifact_refs=_artifacts(("w12-draft", "Draft"), ("w12-qa", "QA report")),
        published_url=None,
        allowed_actions=["pause_week", "open_artifact", "view_audit"],
    )


def _human_review() -> dict[str, Any]:
    return _ready(
        "human_review",
        week_id="W12",
        stage="human_review",
        next_action="Human review required before any publish step",
        qa_status="PASS",
        risk_class="MEDIUM",
        approval_required=True,
        publication_status="NOT_PUBLISHED",
        publication_truth="NOT_PUBLISHED",
        verification_status="NOT_APPLICABLE",
        last_run_at="2026-09-28T12:00:00Z",
        next_schedule_at=None,
        failure=None,
        artifact_refs=_artifacts(("w12-draft", "Draft")),
        published_url=None,
        allowed_actions=["approve", "reject", "request_changes", "open_artifact", "view_audit"],
    )


def _approved_publish_pending() -> dict[str, Any]:
    return _ready(
        "approved_publish_pending",
        week_id="W12",
        stage="publish_pending",
        next_action="Approved. Publish is pending and is not proven.",
        qa_status="PASS",
        risk_class="MEDIUM",
        approval_required=False,
        publication_status="PENDING",
        publication_truth="NOT_PUBLISHED",
        verification_status="NOT_STARTED",
        last_run_at="2026-09-28T13:00:00Z",
        next_schedule_at="2026-09-30T15:00:00Z",
        failure=None,
        artifact_refs=_artifacts(("w12-final", "Final draft")),
        published_url=None,
        allowed_actions=["pause_week", "cancel_run", "open_artifact", "view_audit"],
    )


def _publication_unproven() -> dict[str, Any]:
    return _ready(
        "publication_unproven",
        week_id="W12",
        stage="publication",
        next_action="A URL is on record. Publication is UNPROVEN.",
        qa_status="PASS",
        risk_class="HIGH",
        approval_required=False,
        publication_status="REPORTED",
        publication_truth="UNPROVEN",
        verification_status="UNVERIFIED",
        last_run_at="2026-09-28T14:00:00Z",
        next_schedule_at=None,
        failure=None,
        artifact_refs=_artifacts(("w12-final", "Final draft")),
        published_url=_SAMPLE_URL,
        allowed_actions=[
            "open_published_url",
            "open_artifact",
            "view_audit",
            "retry_failed_stage",
        ],
    )


def _verification_pending() -> dict[str, Any]:
    return _ready(
        "verification_pending",
        week_id="W12",
        stage="verification",
        next_action="Verification is pending. Do not treat the week as verified.",
        qa_status="PASS",
        risk_class="MEDIUM",
        approval_required=False,
        publication_status="SUBMITTED",
        publication_truth="UNVERIFIED",
        verification_status="PENDING",
        last_run_at="2026-09-28T15:00:00Z",
        next_schedule_at="2026-09-29T18:00:00Z",
        failure=None,
        artifact_refs=_artifacts(("w12-final", "Final draft")),
        published_url=_SAMPLE_URL,
        allowed_actions=["open_published_url", "view_audit", "open_artifact"],
    )


def _verified() -> dict[str, Any]:
    return _ready(
        "verified",
        week_id="W12",
        stage="verified",
        next_action="Publication truth is PUBLISHED and verification is VERIFIED.",
        qa_status="PASS",
        risk_class="LOW",
        approval_required=False,
        publication_status="PUBLISHED",
        publication_truth="PUBLISHED",
        verification_status="VERIFIED",
        last_run_at="2026-09-28T16:00:00Z",
        next_schedule_at=None,
        failure=None,
        artifact_refs=_artifacts(("w12-final", "Final draft")),
        published_url=_SAMPLE_URL,
        allowed_actions=["open_published_url", "open_artifact", "view_audit", "pause_week"],
    )


def _failed() -> dict[str, Any]:
    return _ready(
        "failed",
        week_id="W12",
        stage="failed",
        next_action="The draft stage failed. Nothing was published.",
        qa_status="FAIL",
        risk_class="HIGH",
        approval_required=False,
        publication_status="NOT_PUBLISHED",
        publication_truth="NOT_PUBLISHED",
        verification_status="NOT_APPLICABLE",
        last_run_at="2026-09-28T10:30:00Z",
        next_schedule_at=None,
        failure={
            "stage": "draft",
            "code": "STAGE_FAILED",
            "message": "Draft stage failed. No publication was attempted.",
        },
        artifact_refs=_artifacts(("w12-brief", "Week brief")),
        published_url=None,
        allowed_actions=["retry_failed_stage", "view_audit", "open_artifact", "cancel_run"],
    )


def _unknown_remote() -> dict[str, Any]:
    return _ready(
        "unknown_remote",
        week_id="W12",
        stage="UNKNOWN",
        next_action="Remote state is UNKNOWN. Do not treat this week as published.",
        qa_status="UNKNOWN",
        risk_class="UNKNOWN",
        approval_required=False,
        publication_status="UNKNOWN",
        publication_truth="UNKNOWN",
        verification_status="UNKNOWN",
        last_run_at=None,
        next_schedule_at=None,
        failure=None,
        artifact_refs=[],
        published_url=_SAMPLE_URL,
        allowed_actions=["view_audit"],
    )


def _http_401() -> dict[str, Any]:
    return _error(
        "http_401",
        401,
        "UNAUTHORIZED",
        "Read rejected. No Content Ops week was loaded.",
    )


def _http_403() -> dict[str, Any]:
    return _error(
        "http_403",
        403,
        "FORBIDDEN",
        "You are not allowed to read this Content Ops week.",
    )


def _http_409() -> dict[str, Any]:
    return _error(
        "http_409",
        409,
        "ACTIVE_INTENT",
        "An active intent already exists for this week. No new run was started.",
        week_id="W12",
    )


def _http_422() -> dict[str, Any]:
    return _error(
        "http_422",
        422,
        "POLICY_FAILURE",
        "Policy rejected the requested Content Ops action. Nothing was published.",
        policy="human_approval_required",
    )


_SCENARIOS = {
    "empty": _empty,
    "loading": _loading,
    "research_drafting": _research_drafting,
    "qa_pass": _qa_pass,
    "human_review": _human_review,
    "approved_publish_pending": _approved_publish_pending,
    "publication_unproven": _publication_unproven,
    "verification_pending": _verification_pending,
    "verified": _verified,
    "failed": _failed,
    "unknown_remote": _unknown_remote,
    "http_401": _http_401,
    "http_403": _http_403,
    "http_409": _http_409,
    "http_422": _http_422,
}
