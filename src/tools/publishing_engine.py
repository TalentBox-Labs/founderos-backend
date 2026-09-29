"""Marketing OS — Publishing Engine Phase 1 (Sprint M1).

Architecture v2.1 (ADR-002): orchestration ONLY.

- Reads editorially approved bundles
- Validates publish readiness
- Creates publish jobs + queue
- Determines target channel
- Tracks publish state
- Append-only audit
- Manual publish command only

Does NOT: website render, social APIs, email delivery, campaigns,
Celery, n8n, scheduling, or AI publishing.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.tools import editorial_approval as ea
from src.tools import publication_truth as publication_truth
from src.tools.runtime_paths import REPO_ROOT

SCHEMA_VERSION = 1
PUBLISHING_DIR = REPO_ROOT / "output" / "publishing"
JOBS_JSONL = PUBLISHING_DIR / "jobs.jsonl"
AUDIT_JSONL = PUBLISHING_DIR / "audit.jsonl"
JOBS_DIR = PUBLISHING_DIR / "jobs"

# State machine (orchestration layer)
STATE_EDITORIAL_APPROVED = "editorial_approved"  # prerequisite checkpoint
STATE_PUBLISH_PENDING = "publish_pending"
STATE_PUBLISHING = "publishing"
STATE_PUBLISHED = "published"
STATE_FAILED = "failed"
STATE_RETRY = "retry"
STATE_CANCELLED = "cancelled"

PUBLISH_STATES = (
    STATE_EDITORIAL_APPROVED,
    STATE_PUBLISH_PENDING,
    STATE_PUBLISHING,
    STATE_PUBLISHED,
    STATE_FAILED,
    STATE_RETRY,
    STATE_CANCELLED,
)

QUEUE_STATES = frozenset(
    {
        STATE_PUBLISH_PENDING,
        STATE_PUBLISHING,
        STATE_FAILED,
        STATE_RETRY,
    }
)

CHANNEL_WEBSITE = "website"
CHANNEL_LINKEDIN = "linkedin"
CHANNEL_TWITTER = "twitter"
CHANNEL_INSTAGRAM = "instagram"
CHANNEL_NEWSLETTER = "newsletter"

REGISTERED_CHANNELS: tuple[str, ...] = (
    CHANNEL_WEBSITE,
    CHANNEL_LINKEDIN,
    CHANNEL_TWITTER,
    CHANNEL_INSTAGRAM,
    CHANNEL_NEWSLETTER,
)

CHANNEL_OWNERS: dict[str, str] = {
    CHANNEL_WEBSITE: "Website Engine",
    CHANNEL_LINKEDIN: "Social Engine",
    CHANNEL_TWITTER: "Social Engine",
    CHANNEL_INSTAGRAM: "Social Engine",
    CHANNEL_NEWSLETTER: "Email Engine",
}

_CONTENT_ID_RE = re.compile(r"^W\d{2}[A-Z]?$", re.IGNORECASE)

# Reuse editorial human-identity gate (no AI publishing).
is_human_requester = ea.is_human_approver


def publishing_dir() -> Path:
    return PUBLISHING_DIR


def jobs_jsonl() -> Path:
    return JOBS_JSONL


def audit_jsonl() -> Path:
    return AUDIT_JSONL


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_content_id(content_id: str) -> str:
    cid = (content_id or "").strip().upper()
    if not _CONTENT_ID_RE.match(cid):
        raise ValueError(f"Invalid content_id: {content_id!r}")
    return cid


def normalize_channel(channel: str) -> str:
    ch = (channel or "").strip().lower()
    if ch not in REGISTERED_CHANNELS:
        raise ValueError(
            f"Unknown channel {channel!r}. Registered: {list(REGISTERED_CHANNELS)}"
        )
    return ch


def list_channels() -> list[dict[str, Any]]:
    return [
        {
            "channel": ch,
            "owner_engine": CHANNEL_OWNERS[ch],
            "adapter": (
                "placeholder" if ch == CHANNEL_WEBSITE else "not_implemented"
            ),
        }
        for ch in REGISTERED_CHANNELS
    ]


def _ensure_dirs() -> None:
    publishing_dir().mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    _ensure_dirs()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _write_job_snapshot(job: dict[str, Any]) -> Path:
    _ensure_dirs()
    path = JOBS_DIR / f"{job['job_id']}.json"
    path.write_text(json.dumps(job, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def append_audit(
    *,
    job_id: str,
    bundle_id: str,
    requested_by: str,
    channel: str,
    state: str,
    errors: list[str] | None = None,
    retry_count: int = 0,
    notes: str = "",
    event: str = "state_change",
) -> dict[str, Any]:
    record = {
        "schema_version": SCHEMA_VERSION,
        "audit_id": str(uuid.uuid4()),
        "event": event,
        "utc_timestamp": utc_now_iso(),
        "job_id": job_id,
        "bundle_id": bundle_id,
        "requested_by": requested_by,
        "channel": channel,
        "state": state,
        "errors": list(errors or []),
        "retry_count": retry_count,
        "notes": notes or "",
    }
    _append_jsonl(audit_jsonl(), record)
    return record


def load_all_jobs() -> list[dict[str, Any]]:
    """Load jobs from snapshots (authoritative) with JSONL fallback."""
    _ensure_dirs()
    jobs: dict[str, dict[str, Any]] = {}
    if JOBS_DIR.is_dir():
        for path in sorted(JOBS_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("job_id"):
                jobs[str(data["job_id"])] = data
    if not jobs and jobs_jsonl().is_file():
        for line in jobs_jsonl().read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("job_id"):
                jobs[str(data["job_id"])] = data
    return sorted(jobs.values(), key=lambda j: str(j.get("created_at") or ""))


def get_job(job_id: str) -> dict[str, Any] | None:
    jid = (job_id or "").strip()
    if not jid:
        return None
    path = JOBS_DIR / f"{jid}.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            return None
    for job in load_all_jobs():
        if job.get("job_id") == jid:
            return job
    return None


def load_audit_for_job(job_id: str) -> list[dict[str, Any]]:
    path = audit_jsonl()
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("job_id") == job_id:
            out.append(rec)
    return out


def has_editorial_approval(content_id: str) -> dict[str, Any] | None:
    """Return latest successful approve record for bundle, if any phase."""
    cid = normalize_content_id(content_id)
    for phase in ("3", "2b", "2a"):
        rec = ea.successful_approve_for_phase(cid, phase)
        if rec is not None:
            return rec
    # Any successful approve regardless of phase order
    for rec in reversed(ea.load_decisions_for(cid)):
        if rec.get("decision") != ea.DECISION_APPROVE:
            continue
        promo = rec.get("promotion") or {}
        if promo.get("ok") is True:
            return rec
    return None


def validate_publish_readiness(content_id: str) -> dict[str, Any]:
    """Validate Editorial Approved prerequisite (orchestration gate only)."""
    cid = normalize_content_id(content_id)
    approval = has_editorial_approval(cid)
    if approval is None:
        return {
            "ok": False,
            "content_id": cid,
            "editorial_approved": False,
            "errors": [
                "Bundle is not editorially approved; "
                "Publishing Engine requires Editorial Engine approval first"
            ],
        }
    return {
        "ok": True,
        "content_id": cid,
        "editorial_approved": True,
        "editorial_decision_id": approval.get("decision_id"),
        "editorial_phase": approval.get("phase"),
        "bundle": approval.get("bundle") or f"input/{cid}",
        "errors": [],
    }


# --- Channel adapters (interfaces only; M1) ---------------------------------


def _adapter_website(job: dict[str, Any]) -> dict[str, Any]:
    """Website channel placeholder — does not render, deploy, or prove publication."""
    return {
        "ok": True,
        "status": "PLACEHOLDER",
        "channel": CHANNEL_WEBSITE,
        "owner_engine": "Website Engine",
        "message": (
            "Website Engine not implemented in M1; "
            "Publishing Engine recorded orchestration only "
            "(no Markdown/HTML/SEO/deploy)."
        ),
        "website_engine_invoked": False,
        "rendering_performed": False,
        "external_http": False,
        "remote_write_acknowledged": False,
        "remote_object_id": "",
        "publication_truth": publication_truth.TRUTH_UNPROVEN,
    }


def _adapter_not_implemented(channel: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    owner = CHANNEL_OWNERS.get(channel, "Unknown")

    def _run(job: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "NOT_IMPLEMENTED",
            "channel": channel,
            "owner_engine": owner,
            "message": (
                f"Channel adapter for {channel!r} is NOT IMPLEMENTED in M1. "
                f"Owned by {owner}; Publishing Engine does not call external APIs."
            ),
            "external_api_called": False,
        }

    return _run


ADAPTERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    CHANNEL_WEBSITE: _adapter_website,
    CHANNEL_LINKEDIN: _adapter_not_implemented(CHANNEL_LINKEDIN),
    CHANNEL_TWITTER: _adapter_not_implemented(CHANNEL_TWITTER),
    CHANNEL_INSTAGRAM: _adapter_not_implemented(CHANNEL_INSTAGRAM),
    CHANNEL_NEWSLETTER: _adapter_not_implemented(CHANNEL_NEWSLETTER),
}


def create_publish_job(
    *,
    content_id: str,
    channel: str,
    requested_by: str,
    notes: str = "",
) -> dict[str, Any]:
    """Create a publish job in publish_pending after editorial approval check."""
    if not is_human_requester(requested_by):
        raise PermissionError(
            "Human requester required. AI/automation cannot create publish jobs."
        )
    cid = normalize_content_id(content_id)
    ch = normalize_channel(channel)
    readiness = validate_publish_readiness(cid)
    if not readiness["ok"]:
        raise LookupError("; ".join(readiness["errors"]))

    job_id = f"pub_{cid}_{ch}_{uuid.uuid4().hex[:10]}"
    now = utc_now_iso()
    job: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "bundle_id": cid,
        "channel": ch,
        "channel_owner": CHANNEL_OWNERS[ch],
        "state": STATE_PUBLISH_PENDING,
        "state_history": [
            {
                "state": STATE_EDITORIAL_APPROVED,
                "utc_timestamp": now,
                "note": "Prerequisite validated",
            },
            {
                "state": STATE_PUBLISH_PENDING,
                "utc_timestamp": now,
                "note": "Job created",
            },
        ],
        "requested_by": requested_by.strip(),
        "notes": notes or "",
        "created_at": now,
        "updated_at": now,
        "retry_count": 0,
        "errors": [],
        "editorial_decision_id": readiness.get("editorial_decision_id"),
        "editorial_phase": readiness.get("editorial_phase"),
        "bundle": readiness.get("bundle"),
        "adapter_result": None,
        "publication_truth": publication_truth.TRUTH_UNPROVEN,
        "verification_status": publication_truth.TRUTH_UNPROVEN,
        "remote_write_outstanding": False,
        "cancelled": False,
        "orchestration_only": True,
        "website_engine": False,
        "social_engine": False,
        "campaign_engine": False,
    }
    _append_jsonl(jobs_jsonl(), job)
    _write_job_snapshot(job)
    append_audit(
        job_id=job_id,
        bundle_id=cid,
        requested_by=requested_by.strip(),
        channel=ch,
        state=STATE_PUBLISH_PENDING,
        errors=[],
        retry_count=0,
        notes=notes or "",
        event="job_created",
    )
    return job


def _transition(
    job: dict[str, Any],
    new_state: str,
    *,
    requested_by: str,
    errors: list[str] | None = None,
    notes: str = "",
    adapter_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    job["state"] = new_state
    job["updated_at"] = utc_now_iso()
    if errors is not None:
        job["errors"] = list(errors)
    if adapter_result is not None:
        job["adapter_result"] = adapter_result
    history = list(job.get("state_history") or [])
    history.append(
        {
            "state": new_state,
            "utc_timestamp": job["updated_at"],
            "note": notes or "",
            "requested_by": requested_by,
        }
    )
    job["state_history"] = history
    _write_job_snapshot(job)
    append_audit(
        job_id=str(job["job_id"]),
        bundle_id=str(job["bundle_id"]),
        requested_by=requested_by,
        channel=str(job["channel"]),
        state=new_state,
        errors=list(job.get("errors") or []),
        retry_count=int(job.get("retry_count") or 0),
        notes=notes or "",
        event="state_change",
    )
    return job


def _refuse_unknown_remote_rewrite(job: dict[str, Any]) -> None:
    """A dispatched remote call with no acknowledgement must not be written again."""
    outstanding = job.get("remote_write_outstanding") is True
    unknown = job.get("publication_truth") == publication_truth.TRUTH_UNKNOWN_REMOTE
    if outstanding or unknown:
        raise ValueError(
            "Remote result is unknown. Refusing another publication write "
            "until reconciliation."
        )


def _apply_adapter_result(
    job: dict[str, Any],
    result: dict[str, Any],
    *,
    requested_by: str,
    notes: str,
) -> dict[str, Any]:
    truth = publication_truth.classify_adapter_result(result)
    job["publication_truth"] = truth
    if truth == publication_truth.TRUTH_REMOTE_WRITE_CONFIRMED:
        job["verification_status"] = publication_truth.TRUTH_VERIFICATION_PENDING
        job["remote_write_outstanding"] = False
        job["remote_object_id"] = str(result.get("remote_object_id") or "").strip()
        return _transition(
            job,
            STATE_PUBLISHED,
            requested_by=requested_by,
            errors=[],
            notes=notes or "Remote write acknowledged. Verification is still pending.",
            adapter_result=result,
        )
    if truth == publication_truth.TRUTH_UNKNOWN_REMOTE:
        job["verification_status"] = publication_truth.TRUTH_UNKNOWN_REMOTE
        job["remote_write_outstanding"] = True
        return _transition(
            job,
            STATE_FAILED,
            requested_by=requested_by,
            errors=[
                "Remote publication result is unknown. "
                "Another write is blocked until reconciliation."
            ],
            notes=notes or "Remote outcome unknown",
            adapter_result=result,
        )
    if truth == publication_truth.TRUTH_UNPROVEN:
        job["verification_status"] = publication_truth.TRUTH_UNPROVEN
        job["remote_write_outstanding"] = False
        return _transition(
            job,
            STATE_FAILED,
            requested_by=requested_by,
            errors=[
                "Publication unproven. Adapter success, HTTP status, "
                "local writes, and rendering flags are not remote publication."
            ],
            notes=notes or "Publication unproven",
            adapter_result=result,
        )
    job["verification_status"] = publication_truth.TRUTH_FAILED
    job["remote_write_outstanding"] = False
    err = str(result.get("message") or result.get("status") or "Publish failed")
    return _transition(
        job,
        STATE_FAILED,
        requested_by=requested_by,
        errors=[err],
        notes=notes or err,
        adapter_result=result,
    )


def manual_publish(job_id: str, *, requested_by: str, notes: str = "") -> dict[str, Any]:
    """Manual publish command — no scheduling, Celery, or AI."""
    if not is_human_requester(requested_by):
        raise PermissionError(
            "Human requester required. AI/automation cannot publish."
        )
    job = get_job(job_id)
    if job is None:
        raise FileNotFoundError(f"Publish job not found: {job_id}")
    _refuse_unknown_remote_rewrite(job)

    state = job.get("state")
    if state == STATE_CANCELLED:
        raise ValueError("Cannot publish a cancelled job")
    if state == STATE_PUBLISHED:
        raise LookupError("Job already published (duplicate publish)")
    if state == STATE_PUBLISHING:
        raise ValueError("Job is already publishing")
    if state not in {
        STATE_PUBLISH_PENDING,
        STATE_FAILED,
        STATE_RETRY,
    }:
        raise ValueError(f"Invalid transition: cannot publish from state {state!r}")

    job = _transition(
        job,
        STATE_PUBLISHING,
        requested_by=requested_by.strip(),
        notes=notes or "Manual publish started",
    )

    channel = str(job["channel"])
    adapter = ADAPTERS.get(channel)
    if adapter is None:
        return _transition(
            job,
            STATE_FAILED,
            requested_by=requested_by.strip(),
            errors=[f"No adapter registered for channel {channel!r}"],
            notes="Adapter missing",
        )

    result = adapter(job)
    if not isinstance(result, dict):
        result = {
            "ok": False,
            "status": "FAILED",
            "message": "Adapter returned a non-object result",
        }
    return _apply_adapter_result(
        job,
        result,
        requested_by=requested_by.strip(),
        notes=notes or "",
    )


def retry_job(job_id: str, *, requested_by: str, notes: str = "") -> dict[str, Any]:
    if not is_human_requester(requested_by):
        raise PermissionError("Human requester required for retry.")
    job = get_job(job_id)
    if job is None:
        raise FileNotFoundError(f"Publish job not found: {job_id}")
    _refuse_unknown_remote_rewrite(job)
    state = job.get("state")
    if state not in {STATE_FAILED, STATE_RETRY}:
        raise ValueError(f"Invalid transition: cannot retry from state {state!r}")
    job["retry_count"] = int(job.get("retry_count") or 0) + 1
    job["errors"] = []
    job = _transition(
        job,
        STATE_RETRY,
        requested_by=requested_by.strip(),
        errors=[],
        notes=notes or "Marked for retry",
    )
    # Immediately attempt manual publish from retry state
    return manual_publish(job_id, requested_by=requested_by, notes=notes or "Retry publish")


def cancel_job(job_id: str, *, requested_by: str, notes: str = "") -> dict[str, Any]:
    if not is_human_requester(requested_by):
        raise PermissionError("Human requester required for cancel.")
    job = get_job(job_id)
    if job is None:
        raise FileNotFoundError(f"Publish job not found: {job_id}")
    state = job.get("state")
    if state in {STATE_PUBLISHED, STATE_CANCELLED, STATE_PUBLISHING}:
        raise ValueError(f"Invalid transition: cannot cancel from state {state!r}")
    job["cancelled"] = True
    return _transition(
        job,
        STATE_CANCELLED,
        requested_by=requested_by.strip(),
        notes=notes or "Cancelled by requester",
    )


def list_queue(*, include_terminal: bool = False) -> list[dict[str, Any]]:
    jobs = load_all_jobs()
    if include_terminal:
        return jobs
    return [j for j in jobs if j.get("state") in QUEUE_STATES or j.get("state") == STATE_PUBLISH_PENDING]
