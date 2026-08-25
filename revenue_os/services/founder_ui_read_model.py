"""UI-D1 — founder-facing read-model composition (no new SoT).

Server-side aggregation for Jinja demo surfaces. Composes frozen APIs/services only.
"""

from __future__ import annotations

import json
import logging
import uuid as uuid_lib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from revenue_os.database import SessionLocal
from revenue_os.models.activity import Activity, ActivityType, MeetingActivity
from revenue_os.models.approvals import ApprovalRequest
from revenue_os.models.automation_state import AgentActionLog
from revenue_os.models.contact import Contact
from revenue_os.models.deal import Deal
from revenue_os.models.organization import Organization
from revenue_os.services.approvals import list_requests, pending_count
from revenue_os.services.acp2_oversight import compose_orchestration_summary
from revenue_os.services.booking_eligibility import (
    STATE_ALREADY_BOOKED,
    STATE_ELIGIBLE,
    STATE_NO_MEETING_INTEREST,
    STATE_PENDING,
    STATE_STOPPED,
)
from revenue_os.services.calendar_executor import CONNECTOR_OUTLOOK, resolve_calendar_connector
from revenue_os.services.commercial_decision_loop import (
    compose_commercial_decision_items,
    summarize_decision_loop,
)
from revenue_os.services.commercial_funnel_intelligence import (
    compose_commercial_funnel_snapshot,
)
from revenue_os.services.command_operating_surface import (
    attach_command_actions,
    summarize_command_actions,
)
from revenue_os.services.command_v2_projection import compose_command_v2_projection
from revenue_os.services.command_v2_decisions import (
    compose_decision_detail,
    compose_decisions_queue,
)
from revenue_os.services.command_v2_agents import (
    compose_agent_activity_detail,
    compose_agents_overview,
)
from revenue_os.services.operator_flow_read_model import build_operator_flow_snapshot
from revenue_os.services.revenue_orchestration_service import (
    RevenueOrchestrationError,
    inspect_booking_eligibility,
    inspect_follow_up_eligibility,
    inspect_latest_reply_assessment,
)
from revenue_os.services.tenant_context import TenantContext
from revenue_os.services.tenant_scoped_access import TenantAccessError, get_contact_for_tenant

logger = logging.getLogger(__name__)

QD_HANDOFF = "qualified_demand_handoff"
QD_ACCEPTED = "qualified_demand_accepted"
QD_REJECTED = "qualified_demand_rejected"

_DEMAND_SOURCE_LABELS: dict[str, str] = {
    "web_form": "Website form",
    "campaign": "Campaign",
    "social": "Social",
    "linkedin": "LinkedIn",
    "referral": "Referral",
    "event": "Event",
    "manual": "Manually registered",
    "outreach": "Outreach",
}

ACTION_LABELS: dict[str, str] = {
    "qualified_demand_handoff": "Demand registered",
    "qualified_demand_accepted": "Contact accepted",
    "qualified_demand_rejected": "Demand rejected",
    "rev_orch_research_to_outreach": "Researched",
    "worker_research": "Researched",
    "worker_personalize": "Outreach prepared",
    "send_outreach_email": "Outreach prepared",
    "rev_orch_followup_eligibility": "Follow-up checked",
    "worker_followup_proposal": "Follow-up prepared",
    "rev_orch_reply_assessment": "Reply assessed",
    "worker_reply_analysis": "Reply assessed",
    "commercial_outcome_handoff": "Revenue signal recorded",
    "commercial_outcome_accepted": "Revenue signal accepted",
    "commercial_outcome_rejected": "Revenue signal rejected",
    "contact_status_updated": "Contact status updated",
    "deal_stage_updated": "Deal stage updated",
    "rev_orch_booking_eligibility": "Meeting eligibility checked",
    "worker_booking_proposal": "Meeting proposed",
    "book_meeting": "Meeting proposed",
    "approval_requested": "Approval requested",
    "approval_approved": "Approval granted",
    "approval_rejected": "Approval rejected",
}

_NEXT_ACTION_LABELS: dict[str, str] = {
    "BOOKING_ELIGIBLE": "Review meeting times and submit for approval",
    "QUALIFY": "Review qualification (advisory)",
    "HUMAN_REVIEW": "Needs your review",
    "HANDLE_OBJECTION": "Review the objection (advisory)",
    "PAUSE": "Pause outreach (advisory)",
    "DISQUALIFY": "Review disqualification (advisory)",
    "FOLLOW_UP": "Consider a follow-up (advisory)",
}

_REPLY_TYPE_LABELS: dict[str, str] = {
    "MEETING_INTEREST": "Wants a meeting",
    "OBJECTION": "Raised an objection",
    "OUT_OF_OFFICE": "Out of office",
    "UNSUBSCRIBE": "Asked to stop",
    "QUALIFIED": "Positive commercial signal",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _org_uuid(organization_id: str | None) -> uuid_lib.UUID | None:
    if organization_id is None:
        return None
    try:
        return uuid_lib.UUID(str(organization_id))
    except ValueError:
        return None


def _demand_source_label(source: str | None) -> str:
    if not source:
        return "Source not stored"
    key = str(source).strip().lower().replace("-", "_")
    return _DEMAND_SOURCE_LABELS.get(key, str(source).replace("_", " ").title())


def _qualification_reason_text(marketing_qualification: dict[str, Any] | None) -> str | None:
    if not marketing_qualification:
        return None
    for key in ("reason", "notes", "summary"):
        value = marketing_qualification.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    parts: list[str] = []
    tier = marketing_qualification.get("tier")
    score = marketing_qualification.get("score")
    if isinstance(tier, str) and tier.strip():
        parts.append(f"Marketing qualification: {tier.strip()}")
    if isinstance(score, (int, float)):
        parts.append(f"stored score {score}")
    if not parts:
        return None
    return " · ".join(parts)


def _demand_signal_items(
    *,
    content_attribution: dict[str, Any] | None,
    marketing_qualification: dict[str, Any] | None,
    channel: str | None,
) -> list[str]:
    items: list[str] = []
    if channel:
        items.append(f"Channel: {channel}")
    if content_attribution:
        for key, label in (
            ("utm_source", "UTM source"),
            ("campaign", "Campaign"),
            ("content", "Content"),
            ("source_detail", "Source detail"),
            ("registration_mode", "Registration mode"),
        ):
            value = content_attribution.get(key)
            if isinstance(value, str) and value.strip():
                items.append(f"{label}: {value.strip()}")
            elif value is True:
                items.append(f"{label}: yes")
    if marketing_qualification:
        mode = marketing_qualification.get("qualification_mode")
        if isinstance(mode, str) and mode.strip():
            items.append(f"Qualification mode: {mode.strip()}")
    return items


def _organization_display_name(org_uuid: uuid_lib.UUID) -> str | None:
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.id == org_uuid).first()
        if org is None:
            return None
        name = getattr(org, "name", None)
        return str(name) if name else None
    finally:
        db.close()


def _handoff_payloads_for_org(
    *,
    org_uuid: uuid_lib.UUID,
    demand_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Load QD payloads only for demand IDs already in the scoped snapshot."""
    cleaned = [d for d in demand_ids if d]
    if not cleaned:
        return {}
    db = SessionLocal()
    try:
        rows = (
            db.query(AgentActionLog)
            .filter(AgentActionLog.action_type == QD_HANDOFF)
            .filter(AgentActionLog.organization_id == org_uuid)
            .filter(AgentActionLog.target_id.in_(cleaned))
            .all()
        )
        payloads: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not row.target_id:
                continue
            payload = (row.detail or {}).get("payload")
            if isinstance(payload, dict):
                payloads[str(row.target_id)] = payload
        return payloads
    finally:
        db.close()


def _present_pending_demand(
    row: dict[str, Any],
    *,
    payload: dict[str, Any] | None,
    workspace_name: str | None,
) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    person = payload.get("person") if isinstance(payload.get("person"), dict) else {}
    company_hint = (
        payload.get("company_hint") if isinstance(payload.get("company_hint"), dict) else {}
    )
    mq = (
        payload.get("marketing_qualification")
        if isinstance(payload.get("marketing_qualification"), dict)
        else None
    )
    attr = (
        payload.get("content_attribution")
        if isinstance(payload.get("content_attribution"), dict)
        else None
    )
    source = payload.get("source") or row.get("source")
    channel = payload.get("channel") or row.get("channel")
    reason = _qualification_reason_text(mq)
    out = dict(row)
    out["name"] = out.get("name") or person.get("name")
    out["email"] = out.get("email") or person.get("email")
    out["source"] = source
    out["source_label"] = _demand_source_label(str(source) if source else None)
    out["channel"] = channel
    out["company_hint_name"] = company_hint.get("name") if company_hint else None
    out["qualification_state"] = "system_recommended"
    out["qualification_state_label"] = "System recommended — not a human decision"
    out["qualification_reason"] = reason
    out["qualification_reason_missing"] = reason is None
    out["signals"] = _demand_signal_items(
        content_attribution=attr,
        marketing_qualification=mq,
        channel=str(channel) if channel else None,
    )
    out["human_decision_state"] = "awaiting_intake"
    out["human_decision_label"] = "Needs your decision"
    out["authority_class"] = "SYSTEM_RECOMMENDED"
    if reason:
        out["why_it_matters"] = reason
    else:
        out["why_it_matters"] = (
            "This was handed off as qualified demand. "
            "No extra qualification notes were stored."
        )
    out["what_happens_next"] = (
        "If you accept, this person is added to People. "
        "Marketing cannot send outreach or change deals from here."
    )
    out["workspace_context"] = workspace_name
    out["contact_link"] = out.get("contact_link") or "not_yet_created"
    return out


def _enrich_pending_demands(
    raw: list[dict[str, Any]],
    *,
    organization_id: str | None,
) -> list[dict[str, Any]]:
    org_uuid = _org_uuid(organization_id)
    if org_uuid is None:
        return [_present_pending_demand(row, payload=None, workspace_name=None) for row in raw]
    workspace_name = _organization_display_name(org_uuid)
    demand_ids = [str(row.get("demand_id") or "") for row in raw]
    payloads = _handoff_payloads_for_org(org_uuid=org_uuid, demand_ids=demand_ids)
    return [
        _present_pending_demand(
            row,
            payload=payloads.get(str(row.get("demand_id") or "")),
            workspace_name=workspace_name,
        )
        for row in raw
    ]


def _activity_authority_class(action_type: str | None) -> str | None:
    if action_type == QD_HANDOFF:
        return "SYSTEM_RECOMMENDED"
    if action_type in (QD_ACCEPTED, QD_REJECTED):
        return "HUMAN_DECIDED"
    return None


def _human_action(action_type: str) -> str:
    if action_type in ACTION_LABELS:
        return ACTION_LABELS[action_type]
    if action_type == "book_meeting":
        return "Meeting proposed"
    return action_type.replace("_", " ").title()


def _label_next_action(code: str | None) -> str | None:
    if not code:
        return None
    return _NEXT_ACTION_LABELS.get(str(code), str(code).replace("_", " ").capitalize())


def _label_reply_type(code: str | None) -> str | None:
    if not code:
        return None
    return _REPLY_TYPE_LABELS.get(str(code), str(code).replace("_", " ").capitalize())


def _contact_company_name(contact: Contact) -> str | None:
    company = getattr(contact, "company", None)
    if company is None:
        return None
    if isinstance(company, str):
        return company
    name = getattr(company, "name", None)
    return str(name) if name else None


def _proposal_source_label(requested_by: str | None) -> str:
    if requested_by == "booking_worker":
        return "AI proposal"
    if requested_by:
        return "Proposal"
    return "Proposal"


def _present_approval(item: dict[str, Any]) -> dict[str, Any]:
    enriched = _enrich_book_meeting_approval(item)
    out = dict(enriched)
    action_type = str(out.get("action_type") or "")
    out["action_label"] = _human_action(action_type)
    out["proposal_source_label"] = _proposal_source_label(
        str(out.get("requested_by") or "") or None
    )
    out["requires_human_decision"] = True
    return out


def _attention_for_person(
    *,
    reply: dict[str, Any],
    follow_up: dict[str, Any],
    booking: dict[str, Any],
    pending_approvals: list[dict[str, Any]],
    deals: list[dict[str, Any]],
) -> dict[str, Any]:
    booking_state = str(booking.get("ui_state") or "")
    if any(a.get("action_type") == "book_meeting" for a in pending_approvals) or booking_state == "APPROVAL_PENDING":
        return {
            "reason": "A meeting time is waiting for your approval.",
            "next_action": "Review and approve or reject in Approvals.",
            "requires_human": True,
            "kind": "approval",
        }
    if booking_state == "BOOKED":
        return {
            "reason": "A meeting is booked.",
            "next_action": "Review the confirmation on this page.",
            "requires_human": False,
            "kind": "outcome",
        }
    if reply.get("booking_eligible") or booking_state == "BOOKING_ELIGIBLE":
        return {
            "reason": "This person is eligible for a governed meeting proposal.",
            "next_action": "View availability and submit for approval. Nothing is booked until you approve.",
            "requires_human": True,
            "kind": "booking",
        }
    if reply.get("meeting_interest"):
        return {
            "reason": "Meeting interest was detected. Booking is not confirmed.",
            "next_action": reply.get("recommended_next_action_label")
            or "Review the reply. Eligibility is advisory until you act.",
            "requires_human": True,
            "kind": "reply",
        }
    if follow_up.get("eligible"):
        return {
            "reason": "Follow-up is eligible.",
            "next_action": "Propose a follow-up. Sending still requires approval.",
            "requires_human": True,
            "kind": "follow_up",
        }
    if pending_approvals:
        return {
            "reason": "An AI proposal is waiting for your decision.",
            "next_action": "Open Approvals to approve or reject.",
            "requires_human": True,
            "kind": "approval",
        }
    if deals:
        stage = deals[0].get("stage") or "open"
        return {
            "reason": f"A deal is on this person (stage: {stage}).",
            "next_action": "Review deal summary. Stage changes stay on the governed operator path.",
            "requires_human": True,
            "kind": "deal",
        }
    return {
        "reason": "No urgent commercial signal on this person.",
        "next_action": "Start research and a draft if you want AI to prepare outreach.",
        "requires_human": False,
        "kind": "idle",
    }


def _journey_steps(
    workflow: dict[str, str],
    reply: dict[str, Any],
    booking: dict[str, Any],
) -> list[dict[str, str]]:
    booking_state = str(booking.get("ui_state") or "")
    meeting_state = "pending"
    if booking_state == "BOOKED":
        meeting_state = "done"
    elif booking_state in ("APPROVAL_PENDING", "BOOKING_ELIGIBLE"):
        meeting_state = "active"
    elif reply.get("meeting_interest"):
        meeting_state = "active"
    return [
        {"key": "research", "label": "Research", "state": workflow.get("research") or "pending"},
        {"key": "outreach", "label": "Outreach", "state": workflow.get("draft") or "pending"},
        {"key": "follow_up", "label": "Follow-up", "state": workflow.get("follow_up") or "pending"},
        {"key": "reply", "label": "Reply", "state": workflow.get("reply") or "pending"},
        {"key": "meeting", "label": "Meeting", "state": meeting_state},
    ]


_AI_WORK_ACTORS = frozenset(
    {
        "research_worker",
        "personalization_worker",
        "followup_worker",
        "reply_analysis_worker",
        "booking_worker",
    }
)


def _is_classified_ai_work(item: dict[str, Any]) -> bool:
    """True only when actor/action_type is a known worker or rev_orch_* event.

    Human decisions and generic system logs stay on the timeline, not AI work.
    """
    action = str(item.get("action_type") or "")
    actor = str(item.get("actor") or "")
    if action.startswith("rev_orch_") or action.startswith("worker_"):
        return True
    if actor in _AI_WORK_ACTORS or actor.endswith("_worker"):
        return True
    return False


def _ai_work_summary(
    timeline: list[dict[str, Any]],
    pending_approvals: list[dict[str, Any]],
    booking: dict[str, Any],
) -> dict[str, Any]:
    completed = [
        t.get("label")
        for t in timeline
        if t.get("label") and _is_classified_ai_work(t)
    ][:8]
    proposed = [_human_action(str(a.get("action_type") or "")) for a in pending_approvals]
    blocked: list[str] = []
    if pending_approvals or str(booking.get("ui_state") or "") == "APPROVAL_PENDING":
        blocked.append("Waiting for your approval")
    if str(booking.get("ui_state") or "") == "NO_CONNECTOR":
        blocked.append("Calendar is not connected")
    if str(booking.get("ui_state") or "") == "CONNECTOR_UNAVAILABLE":
        blocked.append("This calendar connection cannot show availability")
    return {
        "completed": completed,
        "proposed": proposed,
        "blocked": blocked,
    }


def _present_people_row(row: dict[str, Any], *, company_name: str | None) -> dict[str, Any]:
    out = dict(row)
    out["company_name"] = company_name
    rec = out.get("recommendation")
    out["recommendation_label"] = _label_next_action(str(rec) if rec else None) or rec
    if out.get("demand_link") == "linked":
        out["attention_reason"] = "Accepted from demand intake"
    elif out.get("deal_link") == "linked":
        out["attention_reason"] = "Has a linked deal"
    else:
        out["attention_reason"] = "In your people list"
    out["next_action_label"] = "Open person workspace"
    return out


def _format_slot_display(iso: str | None, *, tz_label: str = "UTC") -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        return dt.strftime(f"%a, %b %d, %Y · %I:%M %p {tz_label}")
    except ValueError:
        return str(iso)[:24]


def _latest_booked_meeting(db, contact_id: uuid_lib.UUID) -> dict[str, Any] | None:  # noqa: ANN001
    activity = (
        db.query(Activity)
        .filter(
            Activity.contact_id == contact_id,
            Activity.activity_type == ActivityType.MEETING,
            Activity.status == "completed",
            Activity.direction == "outbound",
        )
        .order_by(Activity.performed_at.desc())
        .first()
    )
    if activity is None:
        return None
    body: dict[str, Any] = {}
    if activity.body:
        try:
            body = json.loads(activity.body)
        except (json.JSONDecodeError, TypeError):
            body = {}
    meeting_row = (
        db.query(MeetingActivity).filter(MeetingActivity.activity_id == activity.id).first()
    )
    return {
        "activity_id": str(activity.id),
        "title": activity.subject or "Booked meeting",
        "slot_start": body.get("selected_slot_start"),
        "slot_end": body.get("selected_slot_end"),
        "slot_start_display": _format_slot_display(body.get("selected_slot_start")),
        "slot_end_display": _format_slot_display(body.get("selected_slot_end")),
        "provider_event_id": body.get("provider_event_id"),
        "connector": body.get("connector"),
        "meeting_url": meeting_row.meeting_url if meeting_row else None,
        "performed_at": activity.performed_at.isoformat() if activity.performed_at else None,
    }


_SAFE_BOOKING_DEFAULTS: dict[str, Any] = {
    "ui_state": "NOT_ELIGIBLE",
    "backend_state": None,
    "eligible": False,
    "message": "",
    "timezone": "UTC",
    "duration_minutes": 30,
    "connector": None,
    "candidate_slots": [],
    "selected_slot": None,
    "approval": None,
    "confirmation": None,
    "pending_approval": None,
    "booked_meeting": None,
}


def ensure_booking_presentation(booking: Any) -> dict[str, Any]:
    """Safe booking presentation shape for templates (not a SoT, not eligibility)."""
    panel = dict(_SAFE_BOOKING_DEFAULTS)
    if not isinstance(booking, dict):
        return panel
    panel.update(booking)
    if panel.get("approval") is None:
        panel["approval"] = panel.get("pending_approval")
    if panel.get("confirmation") is None:
        panel["confirmation"] = panel.get("booked_meeting")
    if not panel.get("timezone"):
        panel["timezone"] = "UTC"
    if not panel.get("duration_minutes"):
        panel["duration_minutes"] = 30
    if not panel.get("ui_state"):
        panel["ui_state"] = "NOT_ELIGIBLE"
    if panel.get("candidate_slots") is None:
        panel["candidate_slots"] = []
    return panel


def attach_safe_booking(workspace: Any) -> Any:
    """Guarantee workspace.booking is a renderable dict. Never invents eligibility."""
    if not isinstance(workspace, dict):
        return workspace
    out = dict(workspace)
    out["booking"] = ensure_booking_presentation(out.get("booking"))
    return out


def _enrich_book_meeting_approval(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("action_type") != "book_meeting":
        return item
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    tz = str(payload.get("timezone") or "UTC")
    enriched = dict(item)
    enriched["booking_display"] = {
        "meeting_title": payload.get("meeting_title") or item.get("title"),
        "slot_start": payload.get("selected_slot_start"),
        "slot_end": payload.get("selected_slot_end"),
        "slot_start_display": _format_slot_display(payload.get("selected_slot_start"), tz_label=tz),
        "slot_end_display": _format_slot_display(payload.get("selected_slot_end"), tz_label=tz),
        "timezone": tz,
        "duration_minutes": payload.get("duration_minutes"),
        "purpose": payload.get("meeting_notes") or item.get("description"),
        "proposal_source": (
            "AI proposal" if item.get("requested_by") == "booking_worker" else "Proposal"
        ),
        "requested_by_label": (
            "AI proposal" if item.get("requested_by") == "booking_worker" else "Proposal"
        ),
    }
    return enriched


def _build_booking_panel(
    db,  # noqa: ANN001
    tenant: TenantContext,
    contact: Contact,
    organization_id: str,
    contact_approvals: list[dict[str, Any]],
) -> dict[str, Any]:
    """Server-authoritative booking presentation state (not SoT)."""
    panel: dict[str, Any] = dict(_SAFE_BOOKING_DEFAULTS)
    try:
        eligibility = inspect_booking_eligibility(db, tenant, str(contact.id))
    except RevenueOrchestrationError as exc:
        panel["ui_state"] = "ERROR"
        panel["message"] = str(exc)
        return ensure_booking_presentation(panel)

    backend_state = eligibility.get("state")
    panel["backend_state"] = backend_state
    panel["eligible"] = bool(eligibility.get("eligible"))
    panel["duration_minutes"] = int(eligibility.get("duration_minutes") or 30)
    panel["timezone"] = str(eligibility.get("timezone") or "UTC")

    if backend_state == STATE_ALREADY_BOOKED:
        panel["ui_state"] = "BOOKED"
        panel["booked_meeting"] = _latest_booked_meeting(db, contact.id)
        panel["message"] = "Meeting booked"
        panel["confirmation"] = panel["booked_meeting"]
        return ensure_booking_presentation(panel)

    if backend_state == STATE_PENDING:
        approval_id = eligibility.get("approval_id")
        pending = next((a for a in contact_approvals if a.get("id") == approval_id), None)
        if pending is None and approval_id:
            all_pending = list_requests(status="pending", limit=50, organization_id=organization_id)
            pending = next((a for a in all_pending if a.get("id") == approval_id), None)
        panel["ui_state"] = "APPROVAL_PENDING"
        # Frozen UI contract: this exact negative copy is asserted by INT-D2.
        panel["message"] = "booking workflow pending"
        if pending:
            panel["pending_approval"] = _enrich_book_meeting_approval(pending)
            panel["approval"] = panel["pending_approval"]
        return ensure_booking_presentation(panel)

    if backend_state == STATE_ELIGIBLE:
        resolved = resolve_calendar_connector(organization_id)
        if resolved is None:
            panel["ui_state"] = "NO_CONNECTOR"
            panel["message"] = "Connect a Google Calendar for this workspace to view availability"
            return ensure_booking_presentation(panel)
        connector_name, _config = resolved
        panel["connector"] = connector_name
        if connector_name == CONNECTOR_OUTLOOK:
            panel["ui_state"] = "CONNECTOR_UNAVAILABLE"
            panel["message"] = (
                "Availability is not available for this calendar connection yet."
            )
            return ensure_booking_presentation(panel)
        panel["ui_state"] = "BOOKING_ELIGIBLE"
        panel["message"] = "View tenant-safe availability and submit a booking for approval"
        return ensure_booking_presentation(panel)

    if backend_state in (STATE_NO_MEETING_INTEREST, STATE_STOPPED):
        panel["ui_state"] = "NOT_ELIGIBLE"
        panel["message"] = eligibility.get("reason") or "Not eligible for booking"
        return ensure_booking_presentation(panel)

    panel["ui_state"] = "NOT_ELIGIBLE"
    panel["message"] = eligibility.get("reason") or "Not eligible for booking"
    return ensure_booking_presentation(panel)


def _tenant_from_org(organization_id: str | None) -> TenantContext | None:
    if not organization_id:
        return None
    from revenue_os.services.identity_context import anonymous_identity

    return TenantContext(
        identity=anonymous_identity(),
        organization_id=str(organization_id),
        organization_name="",
        organization_slug="",
        membership_id="",
        membership_role="owner",
        membership_status="active",
    )


def _org_scoped_actions(
    db, *, organization_id: str | None, limit: int = 50
) -> list[dict[str, Any]]:  # noqa: ANN001
    org_uuid = _org_uuid(organization_id)
    if org_uuid is None:
        return []
    query = (
        db.query(AgentActionLog)
        .filter(AgentActionLog.organization_id == org_uuid)
        .order_by(AgentActionLog.created_at.desc())
    )
    rows = query.limit(min(limit, 200)).all()
    events: list[dict[str, Any]] = []
    for row in rows:
        payload = (row.detail or {}).get("payload") if isinstance(row.detail, dict) else None
        demand_source = None
        if isinstance(payload, dict) and row.action_type == QD_HANDOFF:
            demand_source = payload.get("source")
        events.append(
            {
                "id": str(row.id) if row.id else None,
                "label": _human_action(row.action_type or ""),
                "action_type": row.action_type,
                "actor": row.actor,
                "target_type": row.target_type,
                "target_id": row.target_id,
                "status": row.status,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "detail": row.detail or {},
                "source": "agent_log",
                "authority_class": _activity_authority_class(row.action_type),
                "demand_source_label": (
                    _demand_source_label(str(demand_source)) if demand_source else None
                ),
            }
        )
    return events


def _reply_summary_from_assessment(assessment_payload: dict[str, Any] | None) -> dict[str, Any]:
    if not assessment_payload:
        return {
            "state": "empty",
            "message": "No reply assessment yet",
            "reply_type": None,
            "reply_type_label": None,
            "summary": None,
            "recommended_next_action": None,
            "recommended_next_action_label": None,
            "meeting_interest": False,
            "booking_eligible": False,
            "booking_status": "none",
        }
    detail = assessment_payload if isinstance(assessment_payload, dict) else {}
    inner = detail.get("assessment") if isinstance(detail.get("assessment"), dict) else {}
    routing = detail.get("routing") if isinstance(detail.get("routing"), dict) else {}
    reply_type = routing.get("reply_type") or inner.get("reply_type")
    meeting_interest = bool(routing.get("meeting_interest") or inner.get("meeting_interest"))
    booking_eligible = bool(routing.get("booking_eligible"))
    recommended = routing.get("recommended_next_action")
    booking_status = "none"
    if meeting_interest or reply_type == "MEETING_INTEREST":
        booking_status = "eligible" if booking_eligible else "interest_detected"
    return {
        "state": "ok",
        "message": "",
        "reply_type": reply_type,
        "summary": inner.get("summary"),
        "confidence": inner.get("confidence") or routing.get("confidence"),
        "recommended_next_action": recommended,
        "recommended_next_action_label": _label_next_action(recommended),
        "reply_type_label": _label_reply_type(reply_type),
        "meeting_interest": meeting_interest,
        "booking_eligible": booking_eligible,
        "booking_status": booking_status,
        "requires_human_review": bool(routing.get("requires_human_review")),
    }


_EMPTY_DECISION_LOOP: dict[str, int] = {
    "total": 0,
    "requires_founder": 0,
    "ready": 0,
    "completed": 0,
    "informational": 0,
}


def ensure_command_center_snapshot_shape(snapshot: Any) -> dict[str, Any]:
    """Guarantee Command template keys without inventing authority or SoT.

    COS-3 requires decision_loop on /command. Partial/mocked snapshots (UI-D1.5)
    may omit it; fill zeros so the page renders without renaming the contract.
    """
    if not isinstance(snapshot, dict):
        return {
            "generated_at": _utc_now(),
            "state": "unavailable",
            "message": "Invalid command snapshot",
            "pending_approvals": [],
            "pending_approval_count": 0,
            "pending_demands": [],
            "pending_demand_count": 0,
            "pipeline": {"contacts": 0, "deals": 0, "deals_by_stage": {}},
            "recent_replies": [],
            "meeting_interest": [],
            "meeting_booking_pending": [],
            "follow_up_signals": [],
            "recent_activity": [],
            "decision_items": [],
            "decision_loop": dict(_EMPTY_DECISION_LOOP),
            "commercial_funnel": {},
            "agent_orchestration": {"counts": {}},
            "command_action_summary": {},
            "errors": [],
        }

    out = dict(snapshot)
    loop = out.get("decision_loop")
    if not isinstance(loop, dict):
        out["decision_loop"] = dict(_EMPTY_DECISION_LOOP)
    else:
        merged = dict(_EMPTY_DECISION_LOOP)
        merged.update({k: loop[k] for k in _EMPTY_DECISION_LOOP if k in loop})
        # Preserve any extra keys from a full COS-3 loop while ensuring required ones.
        for key, value in loop.items():
            if key not in merged:
                merged[key] = value
        out["decision_loop"] = merged

    if not isinstance(out.get("decision_items"), list):
        out["decision_items"] = []
    if not isinstance(out.get("agent_orchestration"), dict):
        out["agent_orchestration"] = {"counts": {}}
    if not isinstance(out.get("pipeline"), dict):
        out["pipeline"] = {"contacts": 0, "deals": 0, "deals_by_stage": {}}
    else:
        pipe = dict(out["pipeline"])
        pipe.setdefault("contacts", 0)
        pipe.setdefault("deals", 0)
        pipe.setdefault("deals_by_stage", {})
        out["pipeline"] = pipe
    if not isinstance(out.get("commercial_funnel"), dict):
        out["commercial_funnel"] = {}
    if out.get("pending_approval_count") is None:
        out["pending_approval_count"] = len(out.get("pending_approvals") or [])
    if out.get("pending_demand_count") is None:
        out["pending_demand_count"] = len(out.get("pending_demands") or [])
    # I1: presentation-only Command V2 projection (no SoT / no authority).
    org_for_v2 = out.get("organization_id")
    out["command_v2"] = compose_command_v2_projection(
        out,
        organization_id=str(org_for_v2) if org_for_v2 else None,
    )
    # I2 glue: attach existing COS-5 actions / labels onto V2 cards (presentation only).
    out["command_v2"] = _enrich_command_v2_with_decision_actions(out)
    return out


def _enrich_command_v2_with_decision_actions(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Copy existing decision_item actions onto matching V2 cards. No new authority."""
    v2 = snapshot.get("command_v2")
    if not isinstance(v2, dict):
        return {
            "organization_id": snapshot.get("organization_id"),
            "source": "command_v2_projection",
            "persistent": False,
            "fail_closed_reason": "missing_projection",
            "buckets": {
                "NEEDS_YOUR_JUDGMENT": [],
                "BLOCKED_OR_DEGRADED": [],
                "RUNNING_WITHOUT_YOU": [],
                "RECENTLY_CHANGED": [],
            },
            "groups": [],
            "cards": [],
        }
    out = dict(v2)
    items = snapshot.get("decision_items")
    if not isinstance(items, list):
        items = []

    by_item_key: dict[str, dict[str, Any]] = {}
    by_approval: dict[str, dict[str, Any]] = {}
    by_demand: dict[str, dict[str, Any]] = {}
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item_id = raw.get("item_id")
        if item_id:
            by_item_key[f"item:{item_id}"] = raw
        prov = raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
        subject = prov.get("subject_id")
        kind = raw.get("kind")
        if kind == "approval" and subject:
            by_approval[str(subject)] = raw
        if kind == "qualified_demand" and subject:
            by_demand[str(subject)] = raw

    def _match(card: dict[str, Any]) -> dict[str, Any] | None:
        key = card.get("card_key")
        if isinstance(key, str) and key in by_item_key:
            return by_item_key[key]
        if card.get("subject_kind") == "approval_request" and card.get("subject_id"):
            return by_approval.get(str(card["subject_id"]))
        if card.get("subject_kind") == "demand" and card.get("subject_id"):
            return by_demand.get(str(card["subject_id"]))
        return None

    enriched_cards: list[dict[str, Any]] = []
    for card in out.get("cards") or []:
        if not isinstance(card, dict):
            continue
        merged = dict(card)
        di = _match(merged)
        if di is not None:
            if di.get("command_actions"):
                merged["command_actions"] = di.get("command_actions")
            if di.get("person_label"):
                merged["person_label"] = di.get("person_label")
            if di.get("company_label"):
                merged["company_label"] = di.get("company_label")
            if di.get("authority_state_label"):
                merged["authority_state_label"] = di.get("authority_state_label")
            if di.get("outcome"):
                merged["outcome"] = di.get("outcome")
            if not merged.get("href") and di.get("href"):
                merged["href"] = di.get("href")
            if not merged.get("proposed_action") and di.get("proposed_action"):
                merged["proposed_action"] = di.get("proposed_action")
            if not merged.get("reason") and di.get("reason"):
                merged["reason"] = di.get("reason")
        enriched_cards.append(merged)

    buckets = out.get("buckets") if isinstance(out.get("buckets"), dict) else {}
    new_buckets: dict[str, list[dict[str, Any]]] = {}
    by_key = {c.get("card_key"): c for c in enriched_cards}
    for name, lst in buckets.items():
        rebuilt: list[dict[str, Any]] = []
        if isinstance(lst, list):
            for card in lst:
                if not isinstance(card, dict):
                    continue
                rebuilt.append(by_key.get(card.get("card_key"), dict(card)))
        new_buckets[str(name)] = rebuilt

    out["cards"] = enriched_cards
    out["buckets"] = new_buckets
    return out


def build_command_center_snapshot(*, organization_id: str | None = None) -> dict[str, Any]:
    """Founder Command Center — attention-oriented read composition."""
    snapshot: dict[str, Any] = {
        "generated_at": _utc_now(),
        "state": "ok",
        "message": "",
        "pending_approvals": [],
        "pending_approval_count": 0,
        "pending_demands": [],
        "pending_demand_count": 0,
        "pipeline": {"contacts": 0, "deals": 0, "deals_by_stage": {}},
        "recent_replies": [],
        "meeting_interest": [],
        "meeting_booking_pending": [],
        "follow_up_signals": [],
        "recent_activity": [],
        "decision_items": [],
        "decision_loop": dict(_EMPTY_DECISION_LOOP),
        "commercial_funnel": {},
        "agent_orchestration": {
            "counts": {
                "succeeded": 0,
                "blocked": 0,
                "awaiting_human": 0,
                "failed": 0,
                "exhausted": 0,
            },
            "source": "AgentActionLog.acp2_*",
        },
        "command_action_summary": {
            "inline_governed": 0,
            "navigate_governed": 0,
            "information_only": 0,
            "total": 0,
        },
        "errors": [],
    }
    flow_contacts: list[dict[str, Any]] = []
    flow: dict[str, Any] | None = None
    org_uuid = _org_uuid(organization_id)
    if organization_id:
        snapshot["organization_id"] = str(organization_id)
    if org_uuid is None:
        snapshot["state"] = "unavailable"
        snapshot["message"] = "Organization context required"
        return ensure_command_center_snapshot_shape(snapshot)

    try:
        flow = build_operator_flow_snapshot(organization_id=organization_id)
        flow_contacts = flow.get("contacts") or []
        snapshot["pending_demands"] = _enrich_pending_demands(
            flow.get("pending_demands") or [],
            organization_id=organization_id,
        )
        snapshot["pending_demand_count"] = len(snapshot["pending_demands"])
        deals = flow.get("deals") or []
        snapshot["pipeline"]["contacts"] = len(flow_contacts)
        snapshot["pipeline"]["deals"] = len(deals)
        by_stage: dict[str, int] = {}
        for d in deals:
            stage = d.get("stage") or "unknown"
            by_stage[stage] = by_stage.get(stage, 0) + 1
        snapshot["pipeline"]["deals_by_stage"] = by_stage
    except Exception as exc:  # noqa: BLE001
        logger.warning("Command center operator flow unavailable: %s", exc)
        snapshot["errors"].append("operator_flow")
        snapshot["state"] = "partial"

    try:
        pending = [
            _present_approval(a)
            for a in list_requests(status="pending", limit=20, organization_id=organization_id)
        ]
        snapshot["pending_approvals"] = pending
        snapshot["pending_approval_count"] = pending_count(organization_id=organization_id)
        snapshot["meeting_booking_pending"] = [
            a for a in pending if a.get("action_type") == "book_meeting"
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Command center approvals unavailable: %s", exc)
        snapshot["errors"].append("approvals")

    tenant = _tenant_from_org(organization_id)
    db = SessionLocal()
    try:
        snapshot["recent_activity"] = _org_scoped_actions(
            db, organization_id=organization_id, limit=12
        )
        reply_q = (
            db.query(AgentActionLog)
            .filter(AgentActionLog.action_type == "rev_orch_reply_assessment")
            .filter(AgentActionLog.organization_id == org_uuid)
        )
        reply_logs = reply_q.order_by(AgentActionLog.created_at.desc()).limit(8).all()
        for log in reply_logs:
            summary = _reply_summary_from_assessment(log.detail or {})
            snapshot["recent_replies"].append(
                {
                    "contact_id": log.target_id,
                    "created_at": log.created_at.isoformat() if log.created_at else None,
                    **summary,
                }
            )
            if summary.get("meeting_interest") or summary.get("booking_eligible"):
                snapshot["meeting_interest"].append(
                    {
                        "contact_id": log.target_id,
                        "reply_type": summary.get("reply_type"),
                        "booking_status": summary.get("booking_status"),
                        "recommended_next_action": summary.get("recommended_next_action"),
                    }
                )

        if tenant is not None and flow_contacts:
            for c in flow_contacts[:5]:
                cid = c.get("id")
                if not cid:
                    continue
                try:
                    elig = inspect_follow_up_eligibility(db, tenant, str(cid))
                    if elig.get("eligible") or elig.get("state") not in (None, "IDLE"):
                        snapshot["follow_up_signals"].append(
                            {
                                "contact_id": cid,
                                "name": c.get("name") or c.get("email"),
                                "state": elig.get("state"),
                                "eligible": elig.get("eligible"),
                                "reason": elig.get("reason"),
                            }
                        )
                except RevenueOrchestrationError:
                    continue
    except SQLAlchemyError as exc:
        logger.warning("Command center DB unavailable: %s", exc)
        snapshot["errors"].append("database")
        snapshot["state"] = "partial"
    finally:
        db.close()

    snapshot["decision_items"] = compose_commercial_decision_items(
        pending_demands=snapshot["pending_demands"],
        pending_approvals=snapshot["pending_approvals"],
        meeting_interest=snapshot["meeting_interest"],
        follow_up_signals=snapshot["follow_up_signals"],
        recent_activity=snapshot["recent_activity"],
        organization_id=organization_id,
    )
    snapshot["decision_items"] = attach_command_actions(
        snapshot["decision_items"],
        organization_id=organization_id,
    )
    snapshot["decision_loop"] = summarize_decision_loop(snapshot["decision_items"])
    snapshot["command_action_summary"] = summarize_command_actions(
        snapshot["decision_items"]
    )
    snapshot["commercial_funnel"] = compose_commercial_funnel_snapshot(
        organization_id=organization_id,
        operator_flow=flow,
        pending_approvals=snapshot["pending_approvals"],
        pending_demands=snapshot["pending_demands"],
        follow_up_signals=snapshot["follow_up_signals"],
        meeting_interest=snapshot["meeting_interest"],
        recent_activity=snapshot["recent_activity"],
        decision_items=snapshot["decision_items"],
    )
    try:
        _odb = SessionLocal()
        try:
            snapshot["agent_orchestration"] = compose_orchestration_summary(
                _odb, organization_id=str(organization_id)
            )
        finally:
            _odb.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Command center ACP-2 orchestration summary unavailable: %s", exc)
        snapshot["errors"].append("agent_orchestration")
    snapshot["ai_completed_count"] = len(snapshot.get("recent_activity") or [])
    return ensure_command_center_snapshot_shape(snapshot)


def _scoped_company_names_for_people(
    *,
    org_uuid: uuid_lib.UUID,
    raw_contacts: list[dict[str, Any]],
) -> dict[str, str | None]:
    """Company names for People rows already in the org-scoped operator snapshot.

    Never enumerates Contact without organization_id. Only loads IDs present
    in the scoped snapshot, then requires Contact.organization_id == org_uuid.
    """
    contact_ids: list[uuid_lib.UUID] = []
    for row in raw_contacts:
        try:
            contact_ids.append(uuid_lib.UUID(str(row.get("id"))))
        except (TypeError, ValueError):
            continue
    if not contact_ids:
        return {}
    db = SessionLocal()
    try:
        rows = (
            db.query(Contact)
            .filter(Contact.organization_id == org_uuid)
            .filter(Contact.id.in_(contact_ids))
            .all()
        )
        return {str(contact.id): _contact_company_name(contact) for contact in rows}
    finally:
        db.close()


def build_demand_contacts_snapshot(*, organization_id: str | None = None) -> dict[str, Any]:
    """Demand + contacts list for founder demo."""
    org_uuid = _org_uuid(organization_id)
    if org_uuid is None:
        return {
            "generated_at": _utc_now(),
            "state": "unavailable",
            "pending_demands": [],
            "contacts": [],
            "message": "Organization context required",
        }
    try:
        flow = build_operator_flow_snapshot(organization_id=str(org_uuid))
        raw_contacts = flow.get("contacts") or []
        company_by_id = _scoped_company_names_for_people(
            org_uuid=org_uuid,
            raw_contacts=raw_contacts,
        )
        people = [
            _present_people_row(
                c, company_name=company_by_id.get(str(c.get("id")))
            )
            for c in raw_contacts
        ]
        pending = _enrich_pending_demands(
            flow.get("pending_demands") or [],
            organization_id=str(org_uuid),
        )
        return {
            "generated_at": _utc_now(),
            "state": "ok",
            "pending_demands": pending,
            "contacts": people,
            "message": "",
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Demand/contacts snapshot unavailable: %s", exc)
        return {
            "generated_at": _utc_now(),
            "state": "unavailable",
            "pending_demands": [],
            "contacts": [],
            "message": "Demand and contact sources unavailable",
        }


def build_contact_workspace_snapshot(
    *,
    organization_id: str | None,
    contact_id: str,
) -> dict[str, Any]:
    """Unified contact revenue workspace."""
    empty: dict[str, Any] = {
        "generated_at": _utc_now(),
        "state": "unavailable",
        "message": "Contact not available",
        "contact": None,
        "deals": [],
        "follow_up": {"state": "empty", "message": "No follow-up data"},
        "reply": _reply_summary_from_assessment(None),
        "booking": ensure_booking_presentation(
            {"ui_state": "NOT_ELIGIBLE", "message": "Organization context required"}
        ),
        "timeline": [],
        "workflow": {
            "research": "unknown",
            "draft": "unknown",
            "approval": "unknown",
            "send": "unknown",
            "follow_up": "unknown",
            "reply": "unknown",
        },
    }
    if not organization_id:
        empty["message"] = "Organization context required"
        return empty

    tenant = _tenant_from_org(organization_id)
    if tenant is None:
        return empty

    db = SessionLocal()
    try:
        try:
            contact = get_contact_for_tenant(db, organization_id, contact_id)
        except TenantAccessError:
            empty["state"] = "not_found"
            empty["message"] = "Contact not found in your organization"
            return empty

        org_uuid = _org_uuid(organization_id)
        deals_q = db.query(Deal).filter(Deal.contact_id == contact.id)
        if org_uuid is not None:
            deals_q = deals_q.filter(Deal.organization_id == org_uuid)
        deals = [
            {
                "id": str(d.id),
                "name": d.name,
                "stage": d.stage.value if d.stage else None,
                "value": float(d.value or 0),
                "currency": d.currency or "USD",
            }
            for d in deals_q.all()
        ]

        follow_up: dict[str, Any] = {"state": "empty", "message": "No follow-up eligibility data"}
        reply = _reply_summary_from_assessment(None)
        try:
            follow_up = inspect_follow_up_eligibility(db, tenant, str(contact.id))
            follow_up["state"] = "ok"
        except RevenueOrchestrationError as exc:
            follow_up = {"state": "unavailable", "message": str(exc)}

        try:
            assessment_payload = inspect_latest_reply_assessment(db, tenant, str(contact.id))
            reply = _reply_summary_from_assessment(assessment_payload.get("assessment"))
            if assessment_payload.get("assessment"):
                reply["raw"] = assessment_payload.get("assessment")
        except RevenueOrchestrationError as exc:
            reply = {"state": "unavailable", "message": str(exc)}

        timeline_q = db.query(AgentActionLog).filter(AgentActionLog.target_id == str(contact.id))
        if org_uuid is not None:
            timeline_q = timeline_q.filter(AgentActionLog.organization_id == org_uuid)
        timeline = [
            {
                "label": _human_action(row.action_type or ""),
                "action_type": row.action_type,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "actor": row.actor,
                "source": "agent_log",
            }
            for row in timeline_q.order_by(AgentActionLog.created_at.desc()).limit(30).all()
        ]

        pending = list_requests(status="pending", limit=50, organization_id=organization_id)
        contact_approvals = [_present_approval(a) for a in pending if a.get("target_id") == str(contact.id)]

        booking = _build_booking_panel(db, tenant, contact, organization_id, contact_approvals)

        workflow = _workflow_stages(timeline, contact_approvals, reply, follow_up)
        presentation = {
            "attention": _attention_for_person(
                reply=reply,
                follow_up=follow_up,
                booking=ensure_booking_presentation(booking),
                pending_approvals=contact_approvals,
                deals=deals,
            ),
            "journey": _journey_steps(workflow, reply, ensure_booking_presentation(booking)),
            "ai_work": _ai_work_summary(
                timeline, contact_approvals, ensure_booking_presentation(booking)
            ),
        }

        return attach_safe_booking({
            "generated_at": _utc_now(),
            "state": "ok",
            "message": "",
            "contact": {
                "id": str(contact.id),
                "name": f"{contact.first_name or ''} {contact.last_name or ''}".strip()
                or contact.email,
                "email": contact.email,
                "status": contact.status.value if contact.status else None,
                "lead_score": contact.lead_score or 0,
                "company": _contact_company_name(contact),
                "phone": contact.phone,
            },
            "deals": deals,
            "follow_up": follow_up,
            "reply": reply,
            "booking": booking,
            "timeline": timeline,
            "pending_approvals": contact_approvals,
            "workflow": workflow,
            "presentation": presentation,
        })
    finally:
        db.close()


def _workflow_stages(
    timeline: list[dict[str, Any]],
    pending_approvals: list[dict[str, Any]],
    reply: dict[str, Any],
    follow_up: dict[str, Any],
) -> dict[str, str]:
    types = {t.get("action_type") for t in timeline}
    stages = {
        "research": "pending",
        "draft": "pending",
        "approval": "pending",
        "send": "pending",
        "follow_up": "pending",
        "reply": "pending",
    }
    if any(t in types for t in ("rev_orch_research_to_outreach",)):
        stages["research"] = "done"
    if pending_approvals:
        stages["draft"] = "active"
        stages["approval"] = "active"
    elif any(t in types for t in ("worker_followup_proposal",)):
        stages["draft"] = "done"
        stages["approval"] = "done"
    if follow_up.get("eligible"):
        stages["follow_up"] = "active"
    elif follow_up.get("state") not in (None, "empty", "unavailable"):
        stages["follow_up"] = "done"
    if reply.get("state") == "ok":
        stages["reply"] = "done"
    return stages


def build_approvals_snapshot(*, organization_id: str | None = None) -> dict[str, Any]:
    """Governed approval inbox + I3 Decisions Queue projection."""
    try:
        if not organization_id:
            return {
                "generated_at": _utc_now(),
                "state": "unavailable",
                "message": "Organization context required",
                "organization_id": None,
                "pending": [],
                "recent": [],
                "pending_count": 0,
                "decisions_queue": compose_decisions_queue(
                    organization_id=None,
                    pending_approvals=[],
                    decision_items=[],
                ),
            }
        pending = list_requests(status="pending", limit=100, organization_id=organization_id)
        recent = list_requests(status=None, limit=30, organization_id=organization_id)
        pending = [_present_approval(a) for a in pending]
        recent = [_present_approval(a) for a in recent]
        meeting_pending = [a for a in pending if a.get("action_type") == "book_meeting"]
        # Compose QD items for queue adjacency without inventing Approvals.
        decision_items = compose_commercial_decision_items(
            pending_demands=[],
            pending_approvals=pending,
            meeting_interest=[],
            follow_up_signals=[],
            recent_activity=[],
            organization_id=organization_id,
        )
        # Prefer operator pending demands when available (tenant-scoped).
        try:
            flow = build_operator_flow_snapshot(organization_id=organization_id)
            pending_demands = _enrich_pending_demands(
                flow.get("pending_demands") or [],
                organization_id=organization_id,
            )
            decision_items = compose_commercial_decision_items(
                pending_demands=pending_demands,
                pending_approvals=pending,
                meeting_interest=[],
                follow_up_signals=[],
                recent_activity=[],
                organization_id=organization_id,
            )
        except Exception:  # noqa: BLE001
            pass
        queue = compose_decisions_queue(
            organization_id=organization_id,
            pending_approvals=pending,
            decision_items=decision_items,
        )
        return {
            "generated_at": _utc_now(),
            "state": "ok",
            "message": "",
            "organization_id": str(organization_id),
            "pending": pending,
            "recent": recent,
            "pending_count": len(pending),
            "meeting_pending_count": len(meeting_pending),
            "decisions_queue": queue,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Approvals snapshot unavailable: %s", exc)
        return {
            "generated_at": _utc_now(),
            "state": "unavailable",
            "message": "Approval queue unavailable",
            "organization_id": str(organization_id) if organization_id else None,
            "pending": [],
            "recent": [],
            "pending_count": 0,
            "decisions_queue": compose_decisions_queue(
                organization_id=organization_id,
                pending_approvals=[],
                decision_items=[],
            ),
        }


def build_decision_detail_snapshot(
    *,
    organization_id: str | None = None,
    approval_request_id: str | None = None,
) -> dict[str, Any]:
    """I3 Decision Detail — single ApprovalRequest, tenant-fail-closed."""
    org = str(organization_id).strip() if organization_id else None
    request_id = str(approval_request_id).strip() if approval_request_id else None
    if not org:
        return {
            "generated_at": _utc_now(),
            "state": "unavailable",
            "message": "Organization context required",
            "detail": compose_decision_detail(organization_id=None, approval=None),
        }
    if not request_id:
        return {
            "generated_at": _utc_now(),
            "state": "unavailable",
            "message": "Decision identity required",
            "detail": compose_decision_detail(organization_id=org, approval=None),
        }

    db = SessionLocal()
    try:
        row = (
            db.query(ApprovalRequest)
            .filter(ApprovalRequest.id == request_id)
            .filter(ApprovalRequest.organization_id == org)
            .one_or_none()
        )
        if row is None:
            # Explicit mismatch / missing — fail closed (no cross-tenant leak).
            return {
                "generated_at": _utc_now(),
                "state": "unavailable",
                "message": "Decision not found for this organization",
                "detail": {
                    "organization_id": org,
                    "source": "command_v2_decisions",
                    "persistent": False,
                    "fail_closed_reason": "approval_not_found_for_tenant",
                    "available": False,
                    "actions": [],
                },
            }
        presented = _present_approval(row.to_dict())
        detail = compose_decision_detail(organization_id=org, approval=presented)
        return {
            "generated_at": _utc_now(),
            "state": "ok" if detail.get("available") else "unavailable",
            "message": "",
            "detail": detail,
        }
    except SQLAlchemyError as exc:
        logger.warning("Decision detail unavailable: %s", exc)
        return {
            "generated_at": _utc_now(),
            "state": "unavailable",
            "message": "Decision detail unavailable",
            "detail": compose_decision_detail(organization_id=org, approval=None),
        }
    finally:
        db.close()


def _agents_source_bundle(*, organization_id: str) -> dict[str, Any]:
    """Load existing orch / activity / pending approvals for I4 composition."""
    pending: list[dict[str, Any]] = []
    try:
        pending = [
            _present_approval(a)
            for a in list_requests(
                status="pending", limit=100, organization_id=organization_id
            )
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Agents snapshot pending approvals unavailable: %s", exc)

    orch: dict[str, Any] = {"counts": {}}
    try:
        db = SessionLocal()
        try:
            orch = compose_orchestration_summary(db, organization_id=str(organization_id))
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Agents snapshot orchestration unavailable: %s", exc)

    activity: list[dict[str, Any]] = []
    try:
        db = SessionLocal()
        try:
            activity = _org_scoped_actions(db, organization_id=organization_id, limit=80)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Agents snapshot activity unavailable: %s", exc)

    return {
        "pending_approvals": pending,
        "agent_orchestration": orch if isinstance(orch, dict) else {"counts": {}},
        "recent_activity": activity,
    }


def build_agents_snapshot(*, organization_id: str | None = None) -> dict[str, Any]:
    """I4 Agents Overview — presentation-only, tenant-fail-closed."""
    org = str(organization_id).strip() if organization_id else None
    if not org:
        return {
            "generated_at": _utc_now(),
            "state": "unavailable",
            "message": "Organization context required",
            "organization_id": None,
            "agents_overview": compose_agents_overview(
                organization_id=None,
                agent_orchestration={},
                recent_activity=[],
                pending_approvals=[],
            ),
        }
    bundle = _agents_source_bundle(organization_id=org)
    overview = compose_agents_overview(
        organization_id=org,
        agent_orchestration=bundle["agent_orchestration"],
        recent_activity=bundle["recent_activity"],
        pending_approvals=bundle["pending_approvals"],
    )
    return {
        "generated_at": _utc_now(),
        "state": "ok",
        "message": "",
        "organization_id": org,
        "agents_overview": overview,
    }


def build_agent_activity_detail_snapshot(
    *,
    organization_id: str | None = None,
    activity_key: str | None = None,
) -> dict[str, Any]:
    """I4 Agent Activity Detail — presentation-only, tenant-fail-closed."""
    org = str(organization_id).strip() if organization_id else None
    if not org:
        return {
            "generated_at": _utc_now(),
            "state": "unavailable",
            "message": "Organization context required",
            "detail": compose_agent_activity_detail(
                organization_id=None,
                activity_key=activity_key,
                agent_orchestration={},
                recent_activity=[],
                pending_approvals=[],
            ),
        }
    bundle = _agents_source_bundle(organization_id=org)
    detail = compose_agent_activity_detail(
        organization_id=org,
        activity_key=activity_key,
        agent_orchestration=bundle["agent_orchestration"],
        recent_activity=bundle["recent_activity"],
        pending_approvals=bundle["pending_approvals"],
    )
    return {
        "generated_at": _utc_now(),
        "state": "ok" if detail.get("available") else "unavailable",
        "message": (
            ""
            if detail.get("available")
            else "Agent activity unavailable for this organization"
        ),
        "detail": detail,
    }


def build_activity_snapshot(
    *, organization_id: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """Founder-facing activity / provenance timeline."""
    db = SessionLocal()
    try:
        events = _org_scoped_actions(db, organization_id=organization_id, limit=limit)
        return {
            "generated_at": _utc_now(),
            "state": "ok" if events else "empty",
            "message": "No activity recorded yet" if not events else "",
            "events": events,
        }
    except SQLAlchemyError as exc:
        logger.warning("Activity snapshot DB unavailable: %s", exc)
        return {
            "generated_at": _utc_now(),
            "state": "unavailable",
            "message": "Activity sources unavailable",
            "events": [],
        }
    finally:
        db.close()
