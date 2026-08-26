"""Approval queue service: propose -> human decides -> execute on approval."""

from __future__ import annotations

import logging
import uuid as uuid_lib
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from revenue_os.database import SessionLocal
from revenue_os.models.approvals import ApprovalRequest
from revenue_os.services.activity_log import log_agent_action
from revenue_os.services.approval_request_identity import (
    CAMPAIGN_FAMILIES,
    FAMILY_BOOK_MEETING,
    FAMILY_PROACTIVE_EMAIL,
    STATUS_PENDING,
    STATUS_SUPERSEDED,
    TARGET_TYPE_MARKETING_CAMPAIGN,
    TERMINAL_STATUSES,
    build_logical_key,
    classify_approval_family,
    effect_key_for_approval,
    material_consent_fingerprint,
    resolve_approval_identity,
)
from revenue_os.services.mutation_authority import (
    HumanAuthorityError,
    require_human_mutation_authority,
)
from revenue_os.services.tenant_context import TenantContext
from revenue_os.services.tenant_scoped_access import (
    TenantAccessError,
    get_approval_for_tenant,
    get_contact_for_tenant,
)

logger = logging.getLogger(__name__)

EXECUTION_PHASE_QUEUED = "queued"
EXECUTION_PHASE_COMPLETED = "completed"
EXECUTION_PHASE_FAILED = "failed"


# ── Executors ────────────────────────────────────────────────────────────────


def _validate_outbound_payload(db: Session, payload: dict) -> None:
    contact_id = payload.get("contact_id")
    org_id = payload.get("organization_id")
    if not contact_id or not org_id:
        raise ValueError("Outbound payload missing contact_id or organization_id")
    contact = get_contact_for_tenant(db, str(org_id), str(contact_id))
    if payload.get("workflow_kind") == "rev_orch_m2_follow_up":
        _revalidate_follow_up_before_send(db, contact, payload)


def _revalidate_follow_up_before_send(db: Session, contact, payload: dict) -> None:
    from revenue_os.services.follow_up_eligibility import (
        contact_has_stop_tags,
        has_reply_stop_after_activity,
    )

    if contact_has_stop_tags(contact):
        raise ValueError("Contact has do-not-contact / unsubscribe tag — send blocked")

    source_id = str(payload.get("source_activity_id") or "")
    if source_id and has_reply_stop_after_activity(db, contact.id, source_id):
        raise ValueError("Reply recorded since follow-up was proposed — send blocked")


def _execute_send_outreach_email(db: Session, payload: dict) -> dict[str, Any]:
    from revenue_os.integrations.n8n import trigger_workflow
    from revenue_os.models.activity import Activity, ActivityType

    _validate_outbound_payload(db, payload)

    # `effect_key` is the stable approval-derived identity.
    # `idempotency_key` may additionally carry legacy workflow provenance.
    effect_key = payload.get("effect_key") or payload.get("idempotency_key")
    idempotency_key = payload.get("idempotency_key") or effect_key
    n8n_payload = {
        "event": "outreach.approved",
        "contact_id": payload.get("contact_id"),
        "email": payload.get("email"),
        "name": payload.get("name"),
        "template": payload.get("template", "intro"),
        "context": payload.get("context", {}),
    }
    if idempotency_key:
        n8n_payload["idempotency_key"] = idempotency_key

    result = trigger_workflow("send-email", n8n_payload)
    delivered = result is not None

    if delivered:
        try:
            cid = uuid_lib.UUID(str(payload.get("contact_id")))
            subject = f"Outbound: {payload.get('template', 'email')}"
            if payload.get("workflow_kind") == "rev_orch_m2_follow_up":
                step = payload.get("follow_up_step")
                subject = f"Follow-up #{step}: {payload.get('template', 'email')}"
            db.add(
                Activity(
                    contact_id=cid,
                    activity_type=ActivityType.EMAIL,
                    subject=subject,
                    body=(payload.get("context") or {}).get("body", "")[:2000],
                    direction="outbound",
                    status="completed",
                )
            )
            db.flush()
        except (ValueError, TypeError):
            pass

    return {
        "handed_to_n8n": delivered,
        "effect_key": effect_key,
        "idempotency_key": idempotency_key,
        "note": None if delivered else "n8n unreachable — check n8n and retry",
    }


def _execute_create_deal(db: Session, payload: dict) -> dict[str, Any]:
    from revenue_os.services.deal_automation_service import create_deal_from_contact

    org_id = str(payload.get("organization_id") or "")
    contact_id = str(payload.get("contact_id") or "")
    if not org_id or not contact_id:
        return {"created": False, "note": "missing organization_id or contact_id"}
    contact = get_contact_for_tenant(db, org_id, contact_id)
    deal = create_deal_from_contact(db, contact, value=float(payload.get("value", 5000.0)))
    db.commit()
    if deal is None:
        return {"created": False, "note": "contact not qualified or deal exists"}
    return {"created": True, "deal_id": str(deal.id)}


def _execute_send_linkedin_message(db: Session, payload: dict) -> dict[str, Any]:
    return {
        "delivery": "manual",
        "note": "LinkedIn has no compliant send API — copy the approved text and send it yourself.",
        "connection_note": payload.get("connection_note"),
        "follow_up_dm": payload.get("follow_up_dm"),
        "effect_key": payload.get("effect_key"),
    }


def _execute_book_meeting(db: Session, payload: dict) -> dict[str, Any]:
    import json

    from revenue_os.models.activity import Activity, ActivityType, MeetingActivity
    from revenue_os.services.booking_eligibility import revalidate_booking_execution
    from revenue_os.services.calendar_executor import create_tenant_calendar_event

    org_id = str(payload.get("organization_id") or "")
    contact_id = str(payload.get("contact_id") or "")
    if not org_id or not contact_id:
        raise ValueError("Booking payload missing organization_id or contact_id")

    contact = get_contact_for_tenant(db, org_id, contact_id)
    revalidate_booking_execution(db, contact, org_id, payload)

    effect_key = payload.get("effect_key") or payload.get("idempotency_key")
    if effect_key:
        existing = (
            db.query(Activity)
            .filter(
                Activity.contact_id == contact.id,
                Activity.activity_type == ActivityType.MEETING,
                Activity.status == "completed",
                Activity.body.contains(effect_key),
            )
            .first()
        )
        if existing is not None:
            return {
                "executed": True,
                "deduplicated": True,
                "activity_id": str(existing.id),
                "effect_key": effect_key,
                "note": "Meeting already booked for this effect key",
            }

    start_raw = payload.get("selected_slot_start")
    end_raw = payload.get("selected_slot_end")
    if not start_raw or not end_raw:
        raise ValueError("Booking payload missing selected slot")

    start_time = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
    end_time = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)
    if end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=timezone.utc)

    if start_time <= datetime.now(timezone.utc):
        raise ValueError("Selected slot is stale or in the past — booking blocked")

    attendees = [a for a in (payload.get("attendees") or []) if a]
    if contact.email and contact.email not in attendees:
        attendees.append(contact.email)

    calendar_result = create_tenant_calendar_event(
        org_id,
        title=str(payload.get("meeting_title") or "Meeting"),
        description=str(payload.get("meeting_notes") or ""),
        start_time=start_time,
        end_time=end_time,
        attendees=attendees,
        idempotency_key=payload.get("effect_key") or effect_key,
    )

    if not calendar_result.get("ok"):
        return {
            "executed": False,
            "note": calendar_result.get("reason", "Calendar execution failed"),
            "effect_key": effect_key,
        }

    activity = Activity(
        contact_id=contact.id,
        activity_type=ActivityType.MEETING,
        subject=str(payload.get("meeting_title") or "Booked meeting"),
        body=json.dumps(
            {
                "effect_key": effect_key,
                "idempotency_key": effect_key,
                "provider_event_id": calendar_result.get("provider_event_id"),
                "connector": calendar_result.get("connector"),
                "selected_slot_start": start_raw,
                "selected_slot_end": end_raw,
                "workflow_kind": payload.get("workflow_kind"),
            }
        ),
        direction="outbound",
        status="completed",
        performed_at=datetime.now(timezone.utc),
    )
    db.add(activity)
    db.flush()
    db.add(
        MeetingActivity(
            activity_id=activity.id,
            meeting_url=calendar_result.get("meeting_url"),
            duration_minutes=int(payload.get("duration_minutes") or 30),
            notes=str(payload.get("meeting_notes") or "")[:2000],
        )
    )
    db.flush()

    status_before = contact.status.value if contact.status else None
    return {
        "executed": True,
        "provider_event_id": calendar_result.get("provider_event_id"),
        "connector": calendar_result.get("connector"),
        "activity_id": str(activity.id),
        "effect_key": effect_key,
        "contact_status_unchanged": True,
        "status_before": status_before,
    }


EXECUTORS: dict[str, Callable[[Session, dict], dict[str, Any]]] = {
    "send_outreach_email": _execute_send_outreach_email,
    "create_deal": _execute_create_deal,
    "send_linkedin_message": _execute_send_linkedin_message,
    "send_reply_email": _execute_send_outreach_email,
    "book_meeting": _execute_book_meeting,
}


# ── Canonical creation ───────────────────────────────────────────────────────


def _find_pending_by_identity(
    db: Session,
    *,
    organization_id: str,
    approval_family: str,
    logical_key: str,
) -> ApprovalRequest | None:
    return (
        db.query(ApprovalRequest)
        .filter(
            ApprovalRequest.status == STATUS_PENDING,
            ApprovalRequest.organization_id == organization_id,
            ApprovalRequest.approval_family == approval_family,
            ApprovalRequest.logical_key == logical_key,
        )
        .first()
    )


def _supersede_row(db: Session, row: ApprovalRequest, *, note: str) -> None:
    row.status = STATUS_SUPERSEDED
    row.decision_note = note
    row.decided_at = datetime.now(timezone.utc)
    db.add(row)


def _supersede_conflicting_booking_pending(
    db: Session,
    *,
    organization_id: str,
    contact_id: str,
    new_logical_key: str,
) -> list[str]:
    superseded: list[str] = []
    rows = (
        db.query(ApprovalRequest)
        .filter(
            ApprovalRequest.status == STATUS_PENDING,
            ApprovalRequest.organization_id == organization_id,
            ApprovalRequest.approval_family == FAMILY_BOOK_MEETING,
            ApprovalRequest.target_id == contact_id,
            ApprovalRequest.logical_key != new_logical_key,
        )
        .all()
    )
    for row in rows:
        _supersede_row(
            db,
            row,
            note="Superseded by booking proposal with different slot",
        )
        superseded.append(str(row.id))
    return superseded


def get_or_create_pending_approval(
    *,
    requested_by: str,
    action_type: str,
    title: str,
    description: str = "",
    target_type: str | None = None,
    target_id: str | None = None,
    payload: dict[str, Any] | None = None,
    organization_id: str | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    """Canonical pending ApprovalRequest boundary."""
    merged_payload = dict(payload or {})
    org_id, family, logical_key = resolve_approval_identity(
        action_type=action_type,
        organization_id=str(organization_id or merged_payload.get("organization_id") or ""),
        target_id=target_id,
        payload=merged_payload,
        target_type=target_type,
    )
    merged_payload["organization_id"] = org_id
    if family == FAMILY_BOOK_MEETING:
        # With contact-scoped pending identity, we must still require slot fields
        # for a booking approval to be executable/meaningful.
        if not merged_payload.get("selected_slot_start") or not merged_payload.get("selected_slot_end"):
            raise ValueError("Booking approval requires selected_slot_start/end")
    if family in CAMPAIGN_FAMILIES:
        # Bound to a real MarketingCampaign.id — never fabricate from contact_id.
        campaign_id = str(merged_payload.get("campaign_id") or "").strip()
        if not campaign_id and str(target_type or "") == TARGET_TYPE_MARKETING_CAMPAIGN:
            campaign_id = str(target_id or "").strip()
        if not campaign_id:
            raise ValueError("Campaign approval requires campaign_id")
        merged_payload["campaign_id"] = campaign_id
        # Strip accidental contact scoping so broadcast identity cannot drift.
        merged_payload.pop("contact_id", None)
        target_type = TARGET_TYPE_MARKETING_CAMPAIGN
        target_id = campaign_id
    merged_payload.setdefault(
        "material_fingerprint",
        material_consent_fingerprint(family, merged_payload),
    )

    owns_session = db is None
    session = db or SessionLocal()
    superseded_ids: list[str] = []
    try:
        if family in CAMPAIGN_FAMILIES:
            from revenue_os.services.marketing_campaign_approval import (
                require_campaign_bound_to_org,
            )

            require_campaign_bound_to_org(
                session,
                organization_id=org_id,
                campaign_id=str(merged_payload["campaign_id"]),
            )

        fp = material_consent_fingerprint(family, merged_payload)
        result: dict[str, Any] | None = None
        for _attempt in range(3):
            pre_mutation_len = len(superseded_ids)

            existing = _find_pending_by_identity(
                session,
                organization_id=org_id,
                approval_family=family,
                logical_key=logical_key,
            )
            if existing is not None:
                existing_fp = material_consent_fingerprint(
                    family, dict(existing.payload or {})
                )
                if fp == existing_fp:
                    if owns_session:
                        session.commit()
                    return {
                        **existing.to_dict(),
                        "created": False,
                        "deduplicated": True,
                        "superseded_ids": superseded_ids,
                    }
                _supersede_row(
                    session,
                    existing,
                    note="Superseded by materially different consent for same logical identity",
                )
                superseded_ids.append(str(existing.id))

            request = ApprovalRequest(
                organization_id=org_id,
                approval_family=family,
                logical_key=logical_key,
                requested_by=requested_by,
                action_type=action_type,
                title=title,
                description=description,
                target_type=target_type,
                target_id=target_id,
                payload=merged_payload,
                status=STATUS_PENDING,
            )
            session.add(request)
            try:
                if owns_session:
                    session.commit()
                    session.refresh(request)
                else:
                    session.flush()
                    session.refresh(request)
                result = {
                    **request.to_dict(),
                    "created": True,
                    "deduplicated": False,
                    "superseded_ids": superseded_ids,
                }
                break
            except IntegrityError:
                # Roll back the failed insert attempt. Any supersede done in this
                # transaction is also rolled back, so superseded_ids must be rewound.
                session.rollback()
                superseded_ids = superseded_ids[:pre_mutation_len]

                existing = _find_pending_by_identity(
                    session,
                    organization_id=org_id,
                    approval_family=family,
                    logical_key=logical_key,
                )
                if existing is None:
                    raise

                existing_fp = material_consent_fingerprint(
                    family, dict(existing.payload or {})
                )
                if fp == existing_fp:
                    if owns_session:
                        session.commit()
                    return {
                        **existing.to_dict(),
                        "created": False,
                        "deduplicated": True,
                        "superseded_ids": superseded_ids,
                    }

                # Material mismatch: supersede the existing pending row and retry insert.
                _supersede_row(
                    session,
                    existing,
                    note="Superseded after concurrent pending insert due to materially different consent",
                )
                superseded_ids.append(str(existing.id))
                continue
        if result is None:
            raise RuntimeError(
                "Failed to create canonical pending approval after retries"
            )
    finally:
        if owns_session:
            session.close()

    log_agent_action(
        actor=requested_by,
        action_type="approval_requested",
        target_type="approval",
        target_id=result["id"],
        organization_id=org_id,
        detail={
            "title": title,
            "action": action_type,
            "approval_family": family,
            "logical_key": logical_key,
            "superseded_ids": superseded_ids,
        },
    )
    return result


def request_approval(
    requested_by: str,
    action_type: str,
    title: str,
    description: str = "",
    target_type: str | None = None,
    target_id: str | None = None,
    payload: dict[str, Any] | None = None,
    organization_id: str | None = None,
) -> dict[str, Any]:
    """Compatibility facade — delegates to canonical get-or-create."""
    return get_or_create_pending_approval(
        requested_by=requested_by,
        action_type=action_type,
        title=title,
        description=description,
        target_type=target_type,
        target_id=target_id,
        payload=payload,
        organization_id=organization_id,
    )


def find_pending_approval(
    db: Session,
    *,
    organization_id: str,
    approval_family: str,
    logical_key: str,
) -> ApprovalRequest | None:
    return _find_pending_by_identity(
        db,
        organization_id=organization_id,
        approval_family=approval_family,
        logical_key=logical_key,
    )


# ── Decision / effect execution ─────────────────────────────────────────────


def _human_decider(tenant: TenantContext | None, decided_by: str | None) -> str:
    if tenant is not None:
        if not tenant.identity.is_human:
            raise HumanAuthorityError(
                f"{tenant.identity.principal_kind.value} cannot approve outbound actions"
            )
        name = (
            tenant.identity.display_name
            or tenant.identity.email
            or "human"
        )
        return require_human_mutation_authority(name, action="approval decision")
    raise HumanAuthorityError(
        "Session tenant context required for approval decisions — "
        "client-supplied decided_by cannot establish human authority"
    )


def _run_approval_effect(db: Session, request: ApprovalRequest) -> dict[str, Any]:
    effect_key = effect_key_for_approval(str(request.id), request.action_type)
    payload = dict(request.payload or {})
    payload["effect_key"] = effect_key
    # Booking proposals retain workflow idempotency_key for execution revalidation.
    if request.action_type != "book_meeting":
        legacy_idempotency_key = payload.get("idempotency_key")
        if isinstance(legacy_idempotency_key, str) and legacy_idempotency_key.strip():
            # Preserve legacy workflow identity for provenance/backward-compat frozen tests,
            # while still embedding the stable approval_effect identity.
            payload["idempotency_key"] = f"{effect_key}:{legacy_idempotency_key}"
        else:
            payload["idempotency_key"] = effect_key

    prior = dict(request.execution_result or {})
    if prior.get("phase") == EXECUTION_PHASE_COMPLETED and prior.get("effect_completed"):
        return prior

    executor = EXECUTORS.get(request.action_type)
    if executor is None:
        return {
            "executed": False,
            "phase": EXECUTION_PHASE_FAILED,
            "effect_key": effect_key,
            "note": f"no executor for '{request.action_type}' — approval recorded only",
        }
    try:
        result = {
            "executed": True,
            "effect_completed": True,
            "phase": EXECUTION_PHASE_COMPLETED,
            "effect_key": effect_key,
            **executor(db, payload),
        }
        if result.get("handed_to_n8n") is False or result.get("executed") is False:
            result["phase"] = EXECUTION_PHASE_FAILED
            result["effect_completed"] = False
        return result
    except Exception as exc:
        logger.error("Approval execution failed (%s): %s", request.id, exc)
        return {
            "executed": False,
            "effect_completed": False,
            "phase": EXECUTION_PHASE_FAILED,
            "effect_key": effect_key,
            "error": str(exc),
        }


def resume_approval_effect(
    request_id: str,
    *,
    tenant: TenantContext | None = None,
) -> dict[str, Any]:
    """Resume approved/queued (or failed-retryable) effect with stable identity."""
    db = SessionLocal()
    try:
        request = db.get(ApprovalRequest, str(request_id).strip())
        if request is None:
            raise ValueError(f"Approval request not found: {request_id}")
        if tenant is not None:
            get_approval_for_tenant(db, tenant.organization_id, request_id)
        if request.status != "approved":
            raise ValueError(f"Approval not resumable from status {request.status}")
        prior = dict(request.execution_result or {})
        if prior.get("phase") == EXECUTION_PHASE_COMPLETED and prior.get("effect_completed"):
            return request.to_dict()
        request.execution_result = _run_approval_effect(db, request)
        db.commit()
        db.refresh(request)
        return request.to_dict()
    finally:
        db.close()


def decide(
    request_id: str,
    approve: bool,
    decided_by: str = "user",
    note: str | None = None,
    *,
    tenant: TenantContext | None = None,
) -> dict[str, Any]:
    """Apply a human decision. Approval queues effect, commits, then executes."""
    from revenue_os.services.acp4_distributed_claim import (
        release_process_local_claim,
        try_acquire_claim,
    )

    approver = _human_decider(tenant, decided_by if tenant is None else None)

    db = SessionLocal()
    claim_key = None
    claim_backend = None
    result: dict[str, Any]
    try:
        q = db.query(ApprovalRequest).filter(ApprovalRequest.id == str(request_id).strip())
        try:
            request = q.with_for_update().one_or_none()
        except Exception:
            request = q.one_or_none()

        if request is None:
            raise ValueError(f"Approval request not found: {request_id}")

        if tenant is not None:
            try:
                bound = get_approval_for_tenant(db, tenant.organization_id, request_id)
            except TenantAccessError as exc:
                raise ValueError("Approval request not found") from exc
            if bound.id != request.id:
                raise ValueError("Approval request not found")

        if request.status != STATUS_PENDING:
            raise ValueError(f"Request already {request.status}")

        org_id = None
        if tenant is not None:
            org_id = str(tenant.organization_id)
        else:
            org_id = str(request.organization_id or "")
            payload = request.payload or {}
            if payload.get("organization_id"):
                org_id = str(payload.get("organization_id"))

        if not approve:
            request.decided_by = approver
            request.decided_at = datetime.now(timezone.utc)
            request.decision_note = note
            request.status = "rejected"
            db.commit()
            result = request.to_dict()
        else:
            if not org_id:
                raise ValueError("Approval execution requires organization scope")

            payload_org = (request.payload or {}).get("organization_id")
            if payload_org is not None and str(payload_org) != str(org_id):
                raise ValueError("Approval organization mismatch — execution blocked")

            claim = try_acquire_claim(
                db,
                organization_id=str(org_id),
                idempotency_key=f"approval_execute:{request_id}",
            )
            claim_key = claim.claim_key
            claim_backend = claim.backend
            if not claim.acquired:
                raise ValueError(
                    "Approval execution claim unavailable — concurrent decide in progress"
                )

            db.refresh(request)
            if request.status != STATUS_PENDING:
                raise ValueError(f"Request already {request.status}")

            effect_key = effect_key_for_approval(str(request.id), request.action_type)
            prior = dict(request.execution_result or {})
            if prior.get("phase") == EXECUTION_PHASE_COMPLETED and prior.get(
                "effect_completed"
            ):
                db.commit()
                result = request.to_dict()
            else:
                request.decided_by = approver
                request.decided_at = datetime.now(timezone.utc)
                request.decision_note = note
                request.status = "approved"
                request.execution_result = {
                    "phase": EXECUTION_PHASE_QUEUED,
                    "effect_key": effect_key,
                    "effect_completed": False,
                }
                db.commit()
                db.refresh(request)

                effect_db = SessionLocal()
                try:
                    locked = effect_db.get(ApprovalRequest, request.id)
                    if locked is None:
                        raise ValueError("Approval request not found after commit")
                    locked.execution_result = _run_approval_effect(effect_db, locked)
                    effect_db.commit()
                    effect_db.refresh(locked)
                    result = locked.to_dict()
                finally:
                    effect_db.close()
    finally:
        if claim_backend == "process_local_test_only":
            release_process_local_claim(claim_key)
        db.close()

    org_id_log = None
    if tenant is not None:
        org_id_log = tenant.organization_id
    elif isinstance(result.get("payload"), dict):
        org_id_log = result["payload"].get("organization_id")
    elif result.get("organization_id"):
        org_id_log = result.get("organization_id")

    log_agent_action(
        actor=approver,
        action_type="approval_approved" if approve else "approval_rejected",
        target_type="approval",
        target_id=request_id,
        organization_id=str(org_id_log) if org_id_log else None,
        detail={"title": result["title"], "execution": result.get("execution_result")},
    )
    return result


def list_requests(
    status: str | None = None,
    limit: int = 100,
    *,
    organization_id: str | None = None,
) -> list[dict[str, Any]]:
    db = SessionLocal()
    try:
        query = db.query(ApprovalRequest).order_by(ApprovalRequest.created_at.desc())
        if status:
            query = query.filter(ApprovalRequest.status == status)
        if organization_id:
            query = query.filter(ApprovalRequest.organization_id == str(organization_id))
        rows = query.limit(min(limit, 500)).all()
        if organization_id is None:
            return [r.to_dict() for r in rows]
        return [r.to_dict() for r in rows if str(r.organization_id) == str(organization_id)]
    finally:
        db.close()


def pending_count(*, organization_id: str | None = None) -> int:
    db = SessionLocal()
    try:
        q = db.query(ApprovalRequest).filter(ApprovalRequest.status == STATUS_PENDING)
        if organization_id:
            q = q.filter(ApprovalRequest.organization_id == str(organization_id))
        return q.count()
    finally:
        db.close()
