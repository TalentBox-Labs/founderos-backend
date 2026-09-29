"""Target read contract for the Content Ops Beta screen.

The Founder OS backend remains authoritative. This module describes the
projection the UI is allowed to render. It does not publish, schedule, or
decide tenant authority.
"""

from __future__ import annotations

from typing import Any, Protocol

TARGET_READ_CONTRACT = "GET /api/v1/content-ops/weeks/current"

PROJECTION_FIELDS = (
    "week_id",
    "stage",
    "next_action",
    "qa_status",
    "risk_class",
    "approval_required",
    "publication_status",
    "publication_truth",
    "verification_status",
    "last_run_at",
    "next_schedule_at",
    "failure",
    "artifact_refs",
    "published_url",
    "allowed_actions",
)

ACTION_CATALOG: dict[str, dict[str, str]] = {
    "run_now": {"label": "Run now", "group": "week"},
    "pause_week": {"label": "Pause week", "group": "week"},
    "resume": {"label": "Resume", "group": "week"},
    "approve": {"label": "Approve", "group": "decision"},
    "reject": {"label": "Reject", "group": "decision"},
    "request_changes": {"label": "Request changes", "group": "decision"},
    "cancel_run": {"label": "Cancel run", "group": "week"},
    "retry_failed_stage": {"label": "Retry failed stage", "group": "week"},
    "open_artifact": {"label": "Open artifact", "group": "audit"},
    "open_published_url": {"label": "Open published URL", "group": "publication"},
    "view_audit": {"label": "View audit", "group": "audit"},
}

PIPELINE_STEPS: tuple[tuple[str, str], ...] = (
    ("research", "Research"),
    ("drafting", "Drafting"),
    ("qa", "QA"),
    ("human_review", "Human review"),
    ("approved", "Approved"),
    ("publish_pending", "Publish pending"),
    ("publication", "Publication"),
    ("verification", "Verification"),
)

STAGE_TO_STEP = {
    "research": "research",
    "drafting": "drafting",
    "qa": "qa",
    "human_review": "human_review",
    "approved": "approved",
    "publish_pending": "publish_pending",
    "publication": "publication",
    "verification": "verification",
    "verified": "verification",
}

DECISION_ACTION_IDS = ("approve", "reject", "request_changes")

PILL_BY_TONE = {
    "verified": "green",
    "attention": "amber",
    "risk": "red",
    "neutral": "gray",
    "unknown": "gray",
}


class ContentOpsReadPort(Protocol):
    """Replaceable read port. The mock adapter is the Beta stand-in."""

    def read_current_week(self, scenario: str | None = None) -> dict[str, Any]:
        """Return one current-week read, or an error envelope."""


def empty_projection() -> dict[str, Any]:
    projection: dict[str, Any] = {field: None for field in PROJECTION_FIELDS}
    projection["approval_required"] = False
    projection["artifact_refs"] = []
    projection["allowed_actions"] = []
    return projection
