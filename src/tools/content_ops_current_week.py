"""Authoritative Content Ops current-week read.

Selection is the ``active_week`` field of canonical ``data/runtime_config.json``.
``WORKCREW_RUNTIME_CONFIG`` and every request field are ignored.

Publication fields come from publish-job snapshots classified by
``publication_truth``. Tracker text, frontmatter, adapter ``ok``, HTTP status,
local files, and a URL string are not remote proof.

This module only reads. It does not create directories or publish.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from src.tools.editorial_approval import PHASE_FILES
from src.tools.publication_truth import (
    TRUTH_FAILED,
    TRUTH_REMOTE_WRITE_CONFIRMED,
    TRUTH_UNKNOWN_REMOTE,
    TRUTH_UNPROVEN,
    TRUTH_VERIFICATION_PENDING,
    TRUTH_VERIFIED,
    verification_from_readback,
)
from src.tools.runtime_paths import REPO_ROOT

CONTRACT = "GET /api/v1/content-ops/weeks/current"
CURRENT_WEEK_SOURCE = "data/runtime_config.json#active_week"
CURRENT_WEEK_SELECTION_RULE = (
    "Use canonical data/runtime_config.json active_week. "
    "Ignore WORKCREW_RUNTIME_CONFIG, query, header, cookie, and body. "
    "Require one tracker row whose content_id matches. "
    "Otherwise return an empty projection."
)

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

_WEEK_ID_RE = re.compile(r"^W\d{2}[A-Z]?$")
_ACTION_ORDER = (
    "run_now",
    "approve",
    "reject",
    "request_changes",
    "retry",
    "cancel",
    "open_artifact",
    "open_published_url",
    "view_audit",
)
_ARTIFACTS = (
    ("brief", "01_Content_Brief.md", "Brief"),
    ("seo", "02_SEO_Plan.md", "SEO plan"),
    ("research", "03_Research.md", "Research"),
    ("draft", "04_Draft.md", "Draft"),
    ("final", "05_Final.md", "Final"),
    ("design", "06_Design_Brief.md", "Design brief"),
    ("social", "07_Social_Posts.md", "Social posts"),
    ("email", "08_Email_Copy.md", "Email copy"),
    ("checklist", "09_Publish_Checklist.md", "Publish checklist"),
)
_STAGING_CANDIDATES = (
    "output/generated/{week}",
    "output/staging/{week}",
    "staging/{week}",
)
_RETRY_STATES = frozenset({"failed", "retry"})
_CANCEL_STATES = frozenset({"publish_pending", "failed", "retry"})
_EDITORIAL_STAGE = {
    "Draft": "drafting",
    "Review": "human_review",
    "Ready": "qa",
    "Approved": "approved",
    "Rejected": "human_review",
    "Needs Changes": "human_review",
}


def empty_projection() -> dict[str, Any]:
    projection: dict[str, Any] = {field: None for field in PROJECTION_FIELDS}
    projection["approval_required"] = False
    projection["artifact_refs"] = []
    projection["allowed_actions"] = []
    projection["publication_truth"] = TRUTH_UNPROVEN
    projection["verification_status"] = TRUTH_UNPROVEN
    projection["publication_status"] = "not_started"
    projection["next_schedule_at"] = None
    return projection


def read_current_week(root: Path | None = None) -> dict[str, Any]:
    """Return the current-week envelope. ``http_status`` is the HTTP status."""
    base = root or REPO_ROOT
    selected = _select_week(base)
    if selected["http_status"] != 200 or selected["row"] is None:
        return selected["envelope"]

    week_id = str(selected["week_id"])
    row = selected["row"]
    artifacts = _artifact_refs(base, week_id)
    decisions = _decisions_for(base, week_id)
    editorial = _editorial_state(artifacts, decisions)
    jobs = _jobs_for(base, week_id)
    if any(str(job.get("state") or "") == "publishing" for job in jobs):
        return _error(
            409,
            "ACTIVE_INTENT",
            "A publication job for the current week is still in progress.",
        )
    if len({str(job.get("job_id")) for job in jobs}) != len(jobs):
        return _error(
            409,
            "ACTIVE_INTENT",
            "Current week publication jobs are not uniquely identifiable.",
        )

    publication = _publication(jobs)
    projection = empty_projection()
    projection.update(
        {
            "week_id": week_id,
            "stage": _stage(editorial, publication),
            "next_action": _next_action(row, editorial, publication),
            "qa_status": _blank_none(row.get("qa_status")),
            "risk_class": _risk_class(publication["publication_truth"]),
            "approval_required": editorial in {"Review", "Ready", "Needs Changes"},
            "publication_status": publication["publication_status"],
            "publication_truth": publication["publication_truth"],
            "verification_status": publication["verification_status"],
            "last_run_at": _last_run_at(base, week_id),
            "next_schedule_at": None,
            "failure": publication["failure"],
            "artifact_refs": [
                {"id": item["id"], "label": item["label"], "path": item["path"]}
                for item in artifacts
            ],
            "published_url": publication["published_url"],
            "allowed_actions": _allowed_actions(
                base,
                week_id,
                artifacts,
                editorial,
                jobs,
                publication,
            ),
            "publication_job_id": publication["publication_job_id"],
        }
    )
    return _ok("ready", "current_week", projection)


def _select_week(base: Path) -> dict[str, Any]:
    runtime_path = base / "data" / "runtime_config.json"
    if not runtime_path.is_file():
        return _empty_selection("no_current_week")
    try:
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_selection("malformed_current_week")
    if not isinstance(runtime, dict):
        return _empty_selection("malformed_current_week")
    raw = runtime.get("active_week")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return _empty_selection("no_current_week")
    if not isinstance(raw, str):
        return _empty_selection("malformed_current_week")
    week_id = raw.strip().upper()
    if not _safe_week_id(week_id):
        return _empty_selection("malformed_current_week")

    rows = [
        row
        for row in _tracker_rows(base)
        if str(row.get("content_id") or "").strip().upper() == week_id
    ]
    if not rows:
        return _empty_selection("current_week_not_in_tracker")
    if len(rows) != 1:
        return {
            "http_status": 409,
            "week_id": None,
            "row": None,
            "envelope": _error(
                409,
                "ACTIVE_INTENT",
                "The current week id is not unique in the content tracker.",
            ),
        }
    return {
        "http_status": 200,
        "week_id": week_id,
        "row": rows[0],
        "envelope": None,
    }


def _empty_selection(reason: str) -> dict[str, Any]:
    return {
        "http_status": 200,
        "week_id": None,
        "row": None,
        "envelope": _ok("empty", reason, empty_projection()),
    }


def _safe_week_id(week_id: str) -> bool:
    if not week_id or len(week_id) > 8:
        return False
    if any(ch in week_id for ch in "/\\.:"):
        return False
    return _WEEK_ID_RE.fullmatch(week_id) is not None


def _tracker_rows(base: Path) -> list[dict[str, str]]:
    path = base / "tracker.csv"
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _artifact_refs(base: Path, week_id: str) -> list[dict[str, str]]:
    folder = base / "input" / week_id
    refs: list[dict[str, str]] = []
    if not folder.is_dir():
        return refs
    for key, filename, label in _ARTIFACTS:
        path = folder / filename
        if path.is_file():
            refs.append(
                {
                    "id": key,
                    "label": label,
                    "path": f"input/{week_id}/{filename}",
                }
            )
    return refs


def _decisions_for(base: Path, week_id: str) -> list[dict[str, Any]]:
    path = base / "output" / "editorial_decisions" / "decisions.jsonl"
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if str(record.get("content_id") or "").strip().upper() == week_id:
            rows.append(record)
    return rows


def _editorial_state(artifacts: list[dict[str, str]], decisions: list[dict[str, Any]]) -> str:
    present = {item["id"] for item in artifacts}
    last = decisions[-1] if decisions else None
    if last is not None:
        decision = str(last.get("decision") or "")
        if decision == "reject":
            return "Rejected"
        if decision == "request_changes":
            return "Needs Changes"
        if decision == "approve":
            promotion = last.get("promotion") or {}
            if isinstance(promotion, dict) and promotion.get("ok") is True:
                return "Approved"
            return "Review"
    if "draft" in present and "final" not in present:
        return "Draft"
    if "draft" in present or "final" in present:
        return "Review"
    return "Draft"


def _jobs_for(base: Path, week_id: str) -> list[dict[str, Any]]:
    jobs_dir = base / "output" / "publishing" / "jobs"
    if not jobs_dir.is_dir():
        return []
    jobs: list[dict[str, Any]] = []
    for path in sorted(jobs_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if str(data.get("bundle_id") or "").strip().upper() != week_id:
            continue
        jobs.append(data)
    return jobs


def _publication(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    classified = [(_job_truth(job), job) for job in jobs]
    chosen = _choose_job(classified)
    if chosen is None:
        return {
            "publication_truth": TRUTH_UNPROVEN,
            "verification_status": TRUTH_UNPROVEN,
            "publication_status": "not_started",
            "published_url": None,
            "failure": None,
            "publication_job_id": None,
        }
    truth, verification, job = chosen
    status = str(job.get("state") or "not_started")
    if status == "published" and truth not in {
        TRUTH_REMOTE_WRITE_CONFIRMED,
        TRUTH_VERIFIED,
    }:
        status = "not_started"
    return {
        "publication_truth": truth,
        "verification_status": verification,
        "publication_status": status,
        "published_url": _published_url(job, truth),
        "failure": _failure(job, truth),
        "publication_job_id": str(job.get("job_id") or "") or None,
    }


def _job_truth(job: dict[str, Any]) -> tuple[str, str]:
    """Return truth, verification. Stored state names are not proof."""
    stored = str(job.get("publication_truth") or "")
    remote_id = str(job.get("remote_object_id") or "").strip()
    if stored == TRUTH_UNKNOWN_REMOTE or job.get("remote_write_outstanding") is True:
        return TRUTH_UNKNOWN_REMOTE, TRUTH_UNKNOWN_REMOTE
    if _readback_verified(job) and remote_id:
        return TRUTH_VERIFIED, TRUTH_VERIFIED
    if stored == TRUTH_REMOTE_WRITE_CONFIRMED and remote_id:
        return TRUTH_REMOTE_WRITE_CONFIRMED, TRUTH_VERIFICATION_PENDING
    if stored == TRUTH_FAILED:
        return TRUTH_FAILED, TRUTH_FAILED
    return TRUTH_UNPROVEN, TRUTH_UNPROVEN


def _readback_verified(job: dict[str, Any]) -> bool:
    evidence = job.get("readback")
    if not isinstance(evidence, dict):
        evidence = job.get("verification_evidence")
    if not isinstance(evidence, dict):
        return False
    return verification_from_readback(evidence) == TRUTH_VERIFIED


def _choose_job(
    classified: list[tuple[tuple[str, str], dict[str, Any]]],
) -> tuple[str, str, dict[str, Any]] | None:
    if not classified:
        return None
    rank = {
        TRUTH_UNKNOWN_REMOTE: 0,
        TRUTH_FAILED: 1,
        TRUTH_REMOTE_WRITE_CONFIRMED: 2,
        TRUTH_VERIFICATION_PENDING: 2,
        TRUTH_UNPROVEN: 3,
        TRUTH_VERIFIED: 4,
    }
    truth_pair, job = sorted(
        classified,
        key=lambda item: (
            rank.get(item[0][0], 3),
            str(item[1].get("updated_at") or ""),
        ),
    )[0]
    # A verified job must not hide an unverified sibling.
    truths = {pair[0] for pair, _job in classified}
    if TRUTH_VERIFIED in truths and truths - {TRUTH_VERIFIED}:
        if TRUTH_UNKNOWN_REMOTE in truths:
            return TRUTH_UNKNOWN_REMOTE, TRUTH_UNKNOWN_REMOTE, job
        if TRUTH_FAILED in truths and TRUTH_REMOTE_WRITE_CONFIRMED not in truths:
            return TRUTH_FAILED, TRUTH_FAILED, job
        if TRUTH_REMOTE_WRITE_CONFIRMED in truths or TRUTH_UNPROVEN in truths:
            pending = next(
                (
                    (pair, item)
                    for pair, item in classified
                    if pair[0] == TRUTH_REMOTE_WRITE_CONFIRMED
                ),
                None,
            )
            if pending is not None:
                return pending[0][0], pending[0][1], pending[1]
            return TRUTH_UNPROVEN, TRUTH_UNPROVEN, job
    return truth_pair[0], truth_pair[1], job


def _published_url(job: dict[str, Any], truth: str) -> str | None:
    if truth not in {TRUTH_REMOTE_WRITE_CONFIRMED, TRUTH_VERIFIED}:
        return None
    for key in ("published_url", "remote_url"):
        url = _http_url(job.get(key))
        if url:
            return url
    return None


def _http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return text
    return None


def _failure(job: dict[str, Any], truth: str) -> str | None:
    if truth not in {TRUTH_FAILED, TRUTH_UNKNOWN_REMOTE}:
        return None
    errors = job.get("errors")
    if isinstance(errors, list):
        messages = [str(item).strip() for item in errors if str(item).strip()]
        if messages:
            return "; ".join(messages)
    if truth == TRUTH_UNKNOWN_REMOTE:
        return "Remote publication result is unknown."
    return "Publication failed."


def _risk_class(truth: str) -> str:
    if truth == TRUTH_UNKNOWN_REMOTE:
        return "unknown_remote"
    if truth == TRUTH_FAILED:
        return "failed"
    if truth in {TRUTH_REMOTE_WRITE_CONFIRMED, TRUTH_VERIFICATION_PENDING}:
        return "verification_pending"
    if truth == TRUTH_VERIFIED:
        return "none"
    return "unproven"


def _stage(editorial: str, publication: dict[str, Any]) -> str:
    truth = publication["publication_truth"]
    if truth == TRUTH_VERIFIED:
        return "verified"
    if truth in {TRUTH_REMOTE_WRITE_CONFIRMED, TRUTH_VERIFICATION_PENDING}:
        return "verification"
    if truth in {TRUTH_FAILED, TRUTH_UNKNOWN_REMOTE}:
        return "publication"
    status = publication["publication_status"]
    if status == "publish_pending":
        return "publish_pending"
    return _EDITORIAL_STAGE.get(editorial, "drafting")


def _next_action(row: dict[str, str], editorial: str, publication: dict[str, Any]) -> str:
    truth = publication["publication_truth"]
    if truth == TRUTH_UNKNOWN_REMOTE:
        return "Reconcile the unknown remote result before another publication write."
    if truth == TRUTH_VERIFICATION_PENDING or truth == TRUTH_REMOTE_WRITE_CONFIRMED:
        return "Remote write is confirmed. Independent read-back is still pending."
    if truth == TRUTH_FAILED:
        return "Publication failed. A human may retry when the remote result is known."
    if truth == TRUTH_VERIFIED:
        return "Remote read-back matched the confirmed write."
    tracker_next = _blank_none(row.get("next_step"))
    if tracker_next:
        return tracker_next
    if editorial == "Approved":
        return "Editorial approval does not publish. A human must create a publish job."
    if editorial in {"Review", "Ready", "Needs Changes", "Rejected"}:
        return "Human editorial decision required."
    if editorial == "Draft":
        return "Continue the human-triggered draft."
    return "No next action was recorded."


def _allowed_actions(
    base: Path,
    week_id: str,
    artifacts: list[dict[str, str]],
    editorial: str,
    jobs: list[dict[str, Any]],
    publication: dict[str, Any],
) -> list[str]:
    actions: set[str] = {"view_audit"}
    if artifacts:
        actions.add("open_artifact")
    if editorial == "Draft" and publication["publication_truth"] == TRUTH_UNPROVEN:
        if not any(str(job.get("state") or "") == "publish_pending" for job in jobs):
            actions.add("run_now")
    if editorial in {"Review", "Ready", "Needs Changes"}:
        actions.add("reject")
        actions.add("request_changes")
        if _approve_supported(base, week_id):
            actions.add("approve")
    if publication["publication_truth"] != TRUTH_UNKNOWN_REMOTE:
        retryable = [
            job
            for job in jobs
            if str(job.get("state") or "") in _RETRY_STATES
            and job.get("remote_write_outstanding") is not True
            and str(job.get("publication_truth") or "") != TRUTH_UNKNOWN_REMOTE
        ]
        if len(retryable) == 1:
            actions.add("retry")
        cancellable = [
            job
            for job in jobs
            if str(job.get("state") or "") in _CANCEL_STATES
            and job.get("remote_write_outstanding") is not True
        ]
        if len(cancellable) == 1:
            actions.add("cancel")
    if (
        publication["publication_truth"] == TRUTH_VERIFIED
        and publication["verification_status"] == TRUTH_VERIFIED
        and publication["published_url"]
    ):
        actions.add("open_published_url")
    return [name for name in _ACTION_ORDER if name in actions]


def _approve_supported(base: Path, week_id: str) -> bool:
    for template in _STAGING_CANDIDATES:
        folder = base / template.format(week=week_id)
        if not folder.is_dir():
            continue
        for filenames in PHASE_FILES.values():
            if filenames and all((folder / name).is_file() for name in filenames):
                return True
    return False


def _last_run_at(base: Path, week_id: str) -> str | None:
    path = base / "output" / "pipeline_orchestrator_run.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("active_week") or "").strip().upper() != week_id:
        return None
    finished = payload.get("finished_at")
    if isinstance(finished, str) and finished.strip():
        return finished.strip()
    return None


def _blank_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.lower() == "none":
        return None
    return text


def _ok(state: str, reason: str, projection: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "source": "content_ops_publication_truth",
        "live": True,
        "contract": CONTRACT,
        "state": state,
        "http_status": 200,
        "reason": reason,
        "projection": projection,
        "error": None,
        "tenant_model": "EXPLICIT_SINGLE_ORG_BETA_LOCK",
        "selection": {
            "source": CURRENT_WEEK_SOURCE,
            "rule": CURRENT_WEEK_SELECTION_RULE,
        },
    }


def _error(status: int, code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "source": "content_ops_publication_truth",
        "live": True,
        "contract": CONTRACT,
        "state": "error",
        "http_status": status,
        "reason": code.lower(),
        "projection": None,
        "error": {"code": code, "message": message},
        "tenant_model": "EXPLICIT_SINGLE_ORG_BETA_LOCK",
        "selection": {
            "source": CURRENT_WEEK_SOURCE,
            "rule": CURRENT_WEEK_SELECTION_RULE,
        },
    }
