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

import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from revenue_os.database import SessionLocal
from revenue_os.models.publication_attempt import PublicationAttempt
from src.tools import editorial_approval as ea
from src.tools.publication_ledger_guard import (
    ATTEMPT_CLASS_INERT_SENTINEL,
    ATTEMPT_CLASS_PUBLICATION,
    SENTINEL_CHANNEL,
    SENTINEL_CONTENT_ID,
    SENTINEL_CONTENT_VERSION,
    SENTINEL_DESTINATION,
    SENTINEL_STATE,
    SENTINEL_TRUTH,
    require_publication_ledger,
)
from src.tools import publication_truth as publication_truth
from src.tools.runtime_paths import REPO_ROOT

SCHEMA_VERSION = 1
PUBLISHING_DIR = REPO_ROOT / "output" / "publishing"
JOBS_JSONL = PUBLISHING_DIR / "jobs.jsonl"
AUDIT_JSONL = PUBLISHING_DIR / "audit.jsonl"
JOBS_DIR = PUBLISHING_DIR / "jobs"
# Retired filesystem ledger. Production authority is the application database.
ATTEMPTS_DB_NAME = "publication_attempts.sqlite"
_attempt_table_ready = False

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
    snapshot = _read_job_snapshot(jid)
    durable = _load_attempt_by_job_id(jid)
    if snapshot is None and durable is None:
        return None
    if snapshot is None:
        return _job_from_attempt(durable)
    if durable is not None:
        _overlay_attempt(snapshot, durable)
    return snapshot


def _read_job_snapshot(job_id: str) -> dict[str, Any] | None:
    path = JOBS_DIR / f"{job_id}.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            return None
    for job in load_all_jobs():
        if job.get("job_id") == job_id:
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


def canonical_destination(channel: str) -> str:
    """One server destination per registered channel. Not a client field."""
    return f"{normalize_channel(channel)}:default"


def publication_attempt_key(
    *,
    tenant_id: str,
    content_id: str,
    content_version: str,
    channel: str,
    destination: str,
) -> str:
    material = "\n".join(
        (tenant_id, content_id, content_version, channel, destination)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _normalize_tenant_id(tenant_id: str) -> str:
    tenant = (tenant_id or "").strip()
    if not tenant or len(tenant) > 80 or any(ch in tenant for ch in "/\\\n\r\x00"):
        raise ValueError("Publication tenant is not bound")
    return tenant


def _content_version(content_id: str, readiness: dict[str, Any]) -> str:
    """Immutable bytes of the approved final, otherwise the approval decision id."""
    roots: list[Path] = []
    bundle = str(readiness.get("bundle") or "").strip()
    if bundle:
        bundle_path = Path(bundle)
        roots.append(bundle_path if bundle_path.is_absolute() else REPO_ROOT / bundle_path)
    roots.append(REPO_ROOT / "input" / content_id)
    for root in roots:
        final = root / "05_Final.md"
        if final.is_file():
            digest = hashlib.sha256(final.read_bytes()).hexdigest()
            return f"sha256:{digest}"
    decision_id = str(readiness.get("editorial_decision_id") or "").strip()
    if not decision_id:
        raise LookupError("Content version is not bound to an artifact or decision")
    return f"editorial-decision:{decision_id}"


def _attempt_bind() -> Any:
    bind = getattr(SessionLocal, "kw", {}).get("bind")
    if bind is not None:
        return bind
    db = SessionLocal()
    try:
        return db.get_bind()
    finally:
        db.close()


def _ensure_attempt_table() -> None:
    """Create the attempt table once. Uniqueness is the primary key, not this flag."""
    global _attempt_table_ready
    if _attempt_table_ready:
        return
    PublicationAttempt.__table__.create(bind=_attempt_bind(), checkfirst=True)
    _attempt_table_ready = True


def _sqlite_busy_timeout(db: Any) -> None:
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "sqlite":
        db.execute(text("PRAGMA busy_timeout=10000"))


def _load_attempt_by_job_id(job_id: str) -> PublicationAttempt | None:
    _ensure_attempt_table()
    db = SessionLocal()
    try:
        row = (
            db.query(PublicationAttempt)
            .filter(PublicationAttempt.job_id == job_id)
            .one_or_none()
        )
        if row is not None:
            db.expunge(row)
        return row
    finally:
        db.close()


def _load_attempt_by_identity(
    *,
    tenant_id: str,
    content_id: str,
    content_version: str,
    channel: str,
    destination: str,
) -> PublicationAttempt | None:
    _ensure_attempt_table()
    db = SessionLocal()
    try:
        row = (
            db.query(PublicationAttempt)
            .filter(
                PublicationAttempt.tenant_id == tenant_id,
                PublicationAttempt.content_id == content_id,
                PublicationAttempt.content_version == content_version,
                PublicationAttempt.channel == channel,
                PublicationAttempt.destination == destination,
            )
            .one_or_none()
        )
        if row is not None:
            db.expunge(row)
        return row
    finally:
        db.close()


def _overlay_attempt(job: dict[str, Any], row: PublicationAttempt) -> dict[str, Any]:
    """Durable outcome wins over a stale filesystem cache."""
    job["state"] = row.state
    job["publication_truth"] = row.publication_truth
    job["verification_status"] = row.verification_status
    job["remote_write_outstanding"] = bool(row.remote_write_outstanding)
    job["attempt_class"] = row.attempt_class
    job["remote_publishable"] = row.attempt_class != ATTEMPT_CLASS_INERT_SENTINEL
    job["tenant_id"] = row.tenant_id
    job["content_version"] = row.content_version
    job["destination"] = row.destination
    job["channel"] = row.channel
    job["attempt_key"] = publication_attempt_key(
        tenant_id=row.tenant_id,
        content_id=row.content_id,
        content_version=row.content_version,
        channel=row.channel,
        destination=row.destination,
    )
    return job


def _job_from_attempt(row: PublicationAttempt) -> dict[str, Any]:
    """Rebuild a fail-closed job when the filesystem cache did not survive."""
    job = {
        "schema_version": SCHEMA_VERSION,
        "job_id": row.job_id,
        "bundle_id": row.content_id,
        "channel": row.channel,
        "channel_owner": CHANNEL_OWNERS.get(row.channel, ""),
        "state": row.state,
        "state_history": [],
        "requested_by": row.requested_by,
        "notes": "",
        "created_at": row.created_at,
        "updated_at": row.created_at,
        "retry_count": 0,
        "errors": [],
        "editorial_decision_id": None,
        "editorial_phase": None,
        "bundle": None,
        "adapter_result": None,
        "publication_truth": row.publication_truth,
        "verification_status": row.verification_status,
        "remote_write_outstanding": bool(row.remote_write_outstanding),
        "attempt_class": row.attempt_class,
        "remote_publishable": row.attempt_class != ATTEMPT_CLASS_INERT_SENTINEL,
        "adapter_call_count": 0,
        "cancelled": row.state == STATE_CANCELLED,
        "orchestration_only": True,
        "website_engine": False,
        "social_engine": False,
        "campaign_engine": False,
        "tenant_id": row.tenant_id,
        "content_version": row.content_version,
        "destination": row.destination,
        "attempt_key": publication_attempt_key(
            tenant_id=row.tenant_id,
            content_id=row.content_id,
            content_version=row.content_version,
            channel=row.channel,
            destination=row.destination,
        ),
        "idempotent": False,
    }
    return job


def _persist_attempt_state(job: dict[str, Any]) -> None:
    _ensure_attempt_table()
    db = SessionLocal()
    try:
        row = (
            db.query(PublicationAttempt)
            .filter(PublicationAttempt.job_id == str(job["job_id"]))
            .one_or_none()
        )
        if row is None:
            return
        row.state = str(job.get("state") or "")
        row.publication_truth = str(job.get("publication_truth") or "")
        row.verification_status = str(job.get("verification_status") or "")
        row.remote_write_outstanding = bool(job.get("remote_write_outstanding"))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _return_existing_attempt(job_id: str) -> dict[str, Any]:
    existing = get_job(job_id)
    if existing is None:
        raise LookupError("Canonical publication attempt has no job record")
    replay = dict(existing)
    replay["idempotent"] = True
    return replay


def create_publish_job(
    *,
    content_id: str,
    channel: str,
    requested_by: str,
    tenant_id: str,
    notes: str = "",
) -> dict[str, Any]:
    """Create one canonical publication attempt, or replay the existing one.

    Uniqueness is the primary key on publication_attempts in the application
    database. A prior read of the job directory is not the lock. The same
    tenant, content version, channel, and destination cannot insert a second
    row. The filesystem job snapshot is a cache of that row.
    """
    if not is_human_requester(requested_by):
        raise PermissionError(
            "Human requester required. AI/automation cannot create publish jobs."
        )
    require_publication_ledger(SessionLocal)
    tenant = _normalize_tenant_id(tenant_id)
    cid = normalize_content_id(content_id)
    ch = normalize_channel(channel)
    destination = canonical_destination(ch)
    readiness = validate_publish_readiness(cid)
    if not readiness["ok"]:
        raise LookupError("; ".join(readiness["errors"]))
    content_version = _content_version(cid, readiness)
    attempt_key = publication_attempt_key(
        tenant_id=tenant,
        content_id=cid,
        content_version=content_version,
        channel=ch,
        destination=destination,
    )
    job_id = f"pub_{attempt_key[:24]}"
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
        "tenant_id": tenant,
        "content_version": content_version,
        "destination": destination,
        "attempt_key": attempt_key,
        "attempt_class": ATTEMPT_CLASS_PUBLICATION,
        "remote_publishable": True,
        "idempotent": False,
    }
    last_locked: OperationalError | None = None
    for _attempt in range(6):
        _ensure_attempt_table()
        db = SessionLocal()
        try:
            _sqlite_busy_timeout(db)
            db.add(
                PublicationAttempt(
                    tenant_id=tenant,
                    content_id=cid,
                    content_version=content_version,
                    channel=ch,
                    destination=destination,
                    job_id=job_id,
                    created_at=now,
                    requested_by=requested_by.strip(),
                    state=STATE_PUBLISH_PENDING,
                    publication_truth=publication_truth.TRUTH_UNPROVEN,
                    verification_status=publication_truth.TRUTH_UNPROVEN,
                    remote_write_outstanding=False,
                    attempt_class=ATTEMPT_CLASS_PUBLICATION,
                )
            )
            db.commit()
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
        except IntegrityError:
            db.rollback()
            existing = _load_attempt_by_identity(
                tenant_id=tenant,
                content_id=cid,
                content_version=content_version,
                channel=ch,
                destination=destination,
            )
            if existing is None:
                raise LookupError("Canonical publication attempt could not be read")
            return _return_existing_attempt(existing.job_id)
        except OperationalError as exc:
            db.rollback()
            last_locked = exc
            if "locked" not in str(exc).lower():
                raise
            time.sleep(0.05)
        finally:
            db.close()
    if last_locked is not None:
        raise last_locked
    raise LookupError("Canonical publication attempt was not stored")


def create_inert_sentinel_attempt(
    *,
    requested_by: str,
    tenant_id: str,
) -> dict[str, Any]:
    """Create the one inert durability sentinel, or replay it.

    Identity is fixed by the server. The row uses the same uniqueness
    transaction as a publication attempt and is classified ``inert_sentinel``.
    No filesystem cache is written, and no channel adapter is selected.
    """
    if not is_human_requester(requested_by):
        raise PermissionError(
            "Human requester required. AI/automation cannot create publish jobs."
        )
    require_publication_ledger(SessionLocal)
    tenant = _normalize_tenant_id(tenant_id)
    attempt_key = publication_attempt_key(
        tenant_id=tenant,
        content_id=SENTINEL_CONTENT_ID,
        content_version=SENTINEL_CONTENT_VERSION,
        channel=SENTINEL_CHANNEL,
        destination=SENTINEL_DESTINATION,
    )
    job_id = f"pub_{attempt_key[:24]}"
    now = utc_now_iso()
    job: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "bundle_id": SENTINEL_CONTENT_ID,
        "channel": SENTINEL_CHANNEL,
        "channel_owner": "",
        "state": SENTINEL_STATE,
        "state_history": [],
        "requested_by": requested_by.strip(),
        "notes": "",
        "created_at": now,
        "updated_at": now,
        "retry_count": 0,
        "errors": [],
        "editorial_decision_id": None,
        "editorial_phase": None,
        "bundle": None,
        "adapter_result": None,
        "publication_truth": SENTINEL_TRUTH,
        "verification_status": SENTINEL_TRUTH,
        "remote_write_outstanding": False,
        "attempt_class": ATTEMPT_CLASS_INERT_SENTINEL,
        "remote_publishable": False,
        "adapter_call_count": 0,
        "cancelled": False,
        "orchestration_only": True,
        "website_engine": False,
        "social_engine": False,
        "campaign_engine": False,
        "tenant_id": tenant,
        "content_version": SENTINEL_CONTENT_VERSION,
        "destination": SENTINEL_DESTINATION,
        "attempt_key": attempt_key,
        "idempotent": False,
    }
    last_locked: OperationalError | None = None
    for _attempt in range(6):
        _ensure_attempt_table()
        db = SessionLocal()
        try:
            _sqlite_busy_timeout(db)
            db.add(
                PublicationAttempt(
                    tenant_id=tenant,
                    content_id=SENTINEL_CONTENT_ID,
                    content_version=SENTINEL_CONTENT_VERSION,
                    channel=SENTINEL_CHANNEL,
                    destination=SENTINEL_DESTINATION,
                    job_id=job_id,
                    created_at=now,
                    requested_by=requested_by.strip(),
                    state=SENTINEL_STATE,
                    publication_truth=SENTINEL_TRUTH,
                    verification_status=SENTINEL_TRUTH,
                    remote_write_outstanding=False,
                    attempt_class=ATTEMPT_CLASS_INERT_SENTINEL,
                )
            )
            db.commit()
            return job
        except IntegrityError:
            db.rollback()
            existing = _load_attempt_by_identity(
                tenant_id=tenant,
                content_id=SENTINEL_CONTENT_ID,
                content_version=SENTINEL_CONTENT_VERSION,
                channel=SENTINEL_CHANNEL,
                destination=SENTINEL_DESTINATION,
            )
            if existing is None:
                raise LookupError("Canonical publication attempt could not be read")
            if existing.attempt_class != ATTEMPT_CLASS_INERT_SENTINEL:
                raise LookupError("Sentinel identity is already a publication attempt")
            replay = _return_existing_attempt(existing.job_id)
            replay["adapter_call_count"] = 0
            replay["remote_publishable"] = False
            return replay
        except OperationalError as exc:
            db.rollback()
            last_locked = exc
            if "locked" not in str(exc).lower():
                raise
            time.sleep(0.05)
        finally:
            db.close()
    if last_locked is not None:
        raise last_locked
    raise LookupError("Canonical publication attempt was not stored")


def _refuse_inert_sentinel(job: dict[str, Any]) -> None:
    """Sentinel rows are rejected before any adapter lookup."""
    if job.get("attempt_class") == ATTEMPT_CLASS_INERT_SENTINEL or (
        job.get("publication_truth") == SENTINEL_TRUTH
    ):
        raise ValueError("Inert sentinel attempts cannot invoke a publication adapter")


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
    _persist_attempt_state(job)
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
    require_publication_ledger(SessionLocal)
    job = get_job(job_id)
    if job is None:
        raise FileNotFoundError(f"Publish job not found: {job_id}")
    _refuse_inert_sentinel(job)
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

    job["remote_write_outstanding"] = True
    job["publication_truth"] = publication_truth.TRUTH_UNKNOWN_REMOTE
    job["verification_status"] = publication_truth.TRUTH_UNKNOWN_REMOTE
    _persist_attempt_state(job)
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
    require_publication_ledger(SessionLocal)
    job = get_job(job_id)
    if job is None:
        raise FileNotFoundError(f"Publish job not found: {job_id}")
    _refuse_inert_sentinel(job)
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
    require_publication_ledger(SessionLocal)
    job = get_job(job_id)
    if job is None:
        raise FileNotFoundError(f"Publish job not found: {job_id}")
    _refuse_inert_sentinel(job)
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
    jobs = [j for j in load_all_jobs() if j.get("attempt_class") != ATTEMPT_CLASS_INERT_SENTINEL]
    if include_terminal:
        return jobs
    return [j for j in jobs if j.get("state") in QUEUE_STATES or j.get("state") == STATE_PUBLISH_PENDING]
