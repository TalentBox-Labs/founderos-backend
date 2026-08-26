"""Canonical ApprovalRequest identity — family, logical_key, material consent."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

FAMILY_PROACTIVE_EMAIL = "proactive_email"
FAMILY_REPLY_EMAIL = "reply_email"
FAMILY_BOOK_MEETING = "book_meeting"
FAMILY_LINKEDIN_MESSAGE = "linkedin_message"
FAMILY_CREATE_DEAL = "create_deal"
FAMILY_EMAIL_CAMPAIGN = "email_campaign"
FAMILY_WHATSAPP_CAMPAIGN = "whatsapp_campaign"
FAMILY_LINKEDIN_CAMPAIGN = "linkedin_campaign"
FAMILY_SOCIAL_CAMPAIGN = "social_campaign"

TARGET_TYPE_MARKETING_CAMPAIGN = "marketing_campaign"

CAMPAIGN_FAMILIES = frozenset(
    {
        FAMILY_EMAIL_CAMPAIGN,
        FAMILY_WHATSAPP_CAMPAIGN,
        FAMILY_LINKEDIN_CAMPAIGN,
        FAMILY_SOCIAL_CAMPAIGN,
    }
)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_SUPERSEDED = "superseded"

TERMINAL_STATUSES = frozenset(
    {STATUS_APPROVED, STATUS_REJECTED, STATUS_SUPERSEDED}
)


def classify_approval_family(action_type: str) -> str:
    match action_type:
        case "send_outreach_email":
            return FAMILY_PROACTIVE_EMAIL
        case "send_reply_email":
            return FAMILY_REPLY_EMAIL
        case "book_meeting":
            return FAMILY_BOOK_MEETING
        case "send_linkedin_message":
            return FAMILY_LINKEDIN_MESSAGE
        case "create_deal":
            return FAMILY_CREATE_DEAL
        case "send_email_campaign":
            return FAMILY_EMAIL_CAMPAIGN
        case "send_whatsapp_campaign":
            return FAMILY_WHATSAPP_CAMPAIGN
        case "publish_linkedin_post":
            return FAMILY_LINKEDIN_CAMPAIGN
        case "publish_social_post":
            return FAMILY_SOCIAL_CAMPAIGN
        case _:
            raise ValueError(f"Unknown approval action_type: {action_type}")


def normalize_slot_timestamp(raw: str) -> str:
    """Deterministic ISO UTC normalization for booking slot identity."""
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def campaign_logical_key(campaign_id: str) -> str:
    return f"campaign:{campaign_id}"


def _resolve_campaign_id(
    *,
    target_type: str | None,
    target_id: str | None,
    payload: dict[str, Any],
) -> str:
    """Campaign instance id only — never contact_id, recipients, or copy."""
    campaign_id = str(payload.get("campaign_id") or "").strip()
    if not campaign_id and str(target_type or "").strip() == TARGET_TYPE_MARKETING_CAMPAIGN:
        campaign_id = str(target_id or "").strip()
    if not campaign_id:
        raise ValueError(
            "Campaign approval requires campaign_id "
            "(or target_type=marketing_campaign with target_id)"
        )
    return campaign_id


def build_logical_key(
    *,
    approval_family: str,
    organization_id: str,
    target_id: str | None,
    payload: dict[str, Any],
    target_type: str | None = None,
) -> str:
    if approval_family in CAMPAIGN_FAMILIES:
        # Contact identity must never substitute for campaign instance identity.
        return campaign_logical_key(
            _resolve_campaign_id(
                target_type=target_type,
                target_id=target_id,
                payload=payload,
            )
        )

    contact_id = str(payload.get("contact_id") or target_id or "").strip()
    if not contact_id:
        raise ValueError("Approval logical identity requires contact_id or target_id")

    match approval_family:
        case family if family == FAMILY_PROACTIVE_EMAIL:
            return f"contact:{contact_id}"
        case family if family == FAMILY_REPLY_EMAIL:
            source_activity_id = str(payload.get("source_activity_id") or "").strip()
            if not source_activity_id:
                raise ValueError(
                    "Reply approval requires repository-grounded source_activity_id"
                )
            return f"contact:{contact_id}:source_activity:{source_activity_id}"
        case family if family == FAMILY_BOOK_MEETING:
            # Booking pending identity is contact-scoped (slot lives in immutable payload).
            return f"contact:{contact_id}"
        case family if family == FAMILY_LINKEDIN_MESSAGE:
            return f"contact:{contact_id}"
        case family if family == FAMILY_CREATE_DEAL:
            return f"contact:{contact_id}"
        case _:
            raise ValueError(f"Unsupported approval_family: {approval_family}")


def resolve_approval_identity(
    *,
    action_type: str,
    organization_id: str,
    target_id: str | None,
    payload: dict[str, Any],
    target_type: str | None = None,
) -> tuple[str, str, str]:
    org_id = str(organization_id or payload.get("organization_id") or "").strip()
    if not org_id:
        raise ValueError("Approval requires organization_id")
    family = classify_approval_family(action_type)
    logical_key = build_logical_key(
        approval_family=family,
        organization_id=org_id,
        target_id=target_id,
        payload=payload,
        target_type=target_type,
    )
    return org_id, family, logical_key


def effect_key_for_approval(approval_id: str, action_type: str) -> str:
    return f"approval_effect:{approval_id}:{action_type}"


def _material_proactive(payload: dict[str, Any]) -> dict[str, Any]:
    ctx = payload.get("context") or {}
    return {
        "email": payload.get("email"),
        "template": payload.get("template"),
        "body": ctx.get("body") if isinstance(ctx, dict) else None,
    }


def _material_reply(payload: dict[str, Any]) -> dict[str, Any]:
    ctx = payload.get("context") or {}
    return {
        "email": payload.get("email"),
        "source_activity_id": payload.get("source_activity_id"),
        "body": ctx.get("body") if isinstance(ctx, dict) else None,
    }


def _material_booking(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "selected_slot_start": payload.get("selected_slot_start"),
        "selected_slot_end": payload.get("selected_slot_end"),
        "meeting_title": payload.get("meeting_title"),
    }


def _material_linkedin(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "connection_note": payload.get("connection_note"),
        "follow_up_dm": payload.get("follow_up_dm"),
    }


def _material_create_deal(payload: dict[str, Any]) -> dict[str, Any]:
    return {"value": payload.get("value")}


def _material_email_campaign(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "campaign_type": payload.get("campaign_type"),
        "subject": payload.get("subject"),
        "preview_text": payload.get("preview_text"),
        "body": payload.get("body"),
        "cta": payload.get("cta"),
        "recipients": payload.get("recipients"),
    }


def _material_whatsapp_campaign(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "campaign_type": payload.get("campaign_type"),
        "tag": payload.get("tag"),
        "message": payload.get("message"),
    }


def _material_linkedin_campaign(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "body": payload.get("body"),
        "post_type": payload.get("post_type"),
    }


def _material_social_campaign(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "variants": payload.get("variants"),
        "platforms": payload.get("platforms"),
        "topic": payload.get("topic"),
    }


def material_consent_fingerprint(
    approval_family: str, payload: dict[str, Any]
) -> str:
    match approval_family:
        case family if family == FAMILY_PROACTIVE_EMAIL:
            material = _material_proactive(payload)
        case family if family == FAMILY_REPLY_EMAIL:
            material = _material_reply(payload)
        case family if family == FAMILY_BOOK_MEETING:
            material = _material_booking(payload)
        case family if family == FAMILY_LINKEDIN_MESSAGE:
            material = _material_linkedin(payload)
        case family if family == FAMILY_CREATE_DEAL:
            material = _material_create_deal(payload)
        case family if family == FAMILY_EMAIL_CAMPAIGN:
            material = _material_email_campaign(payload)
        case family if family == FAMILY_WHATSAPP_CAMPAIGN:
            material = _material_whatsapp_campaign(payload)
        case family if family == FAMILY_LINKEDIN_CAMPAIGN:
            material = _material_linkedin_campaign(payload)
        case family if family == FAMILY_SOCIAL_CAMPAIGN:
            material = _material_social_campaign(payload)
        case _:
            material = {}
    encoded = json.dumps(material, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def proactive_email_logical_key(contact_id: str) -> str:
    return f"contact:{contact_id}"


def reply_email_logical_key(contact_id: str, source_activity_id: str) -> str:
    return f"contact:{contact_id}:source_activity:{source_activity_id}"


def booking_logical_key(contact_id: str, slot_start: str, slot_end: str) -> str:
    # Backwards-compatible signature: slot is ignored for pending uniqueness.
    return f"contact:{contact_id}"


def backfill_identity_from_row(
    action_type: str,
    target_id: str | None,
    payload: dict[str, Any] | None,
    target_type: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Best-effort identity for migration backfill."""
    payload = dict(payload or {})
    org_id = payload.get("organization_id")
    if not org_id or not action_type:
        return None, None, None
    try:
        family = classify_approval_family(action_type)
        logical_key = build_logical_key(
            approval_family=family,
            organization_id=str(org_id),
            target_id=target_id,
            payload=payload,
            target_type=target_type or payload.get("target_type"),
        )
        return str(org_id), family, logical_key
    except ValueError:
        if action_type == "send_outreach_email" and target_id:
            return str(org_id), FAMILY_PROACTIVE_EMAIL, proactive_email_logical_key(
                str(target_id)
            )
        if action_type == "send_reply_email" and target_id:
            sid = payload.get("source_activity_id")
            if sid:
                return (
                    str(org_id),
                    FAMILY_REPLY_EMAIL,
                    reply_email_logical_key(str(target_id), str(sid)),
                )
        if action_type == "book_meeting" and target_id:
            # Pending identity is contact-scoped; selected_slot_start/end are material
            # consent fields handled by the executor/fingerprint.
            start = payload.get("selected_slot_start") or ""
            end = payload.get("selected_slot_end") or ""
            return (
                str(org_id),
                FAMILY_BOOK_MEETING,
                booking_logical_key(str(target_id), str(start), str(end)),
            )
        if action_type == "send_linkedin_message" and target_id:
            return (
                str(org_id),
                FAMILY_LINKEDIN_MESSAGE,
                proactive_email_logical_key(str(target_id)),
            )
        if action_type == "create_deal" and target_id:
            return (
                str(org_id),
                FAMILY_CREATE_DEAL,
                proactive_email_logical_key(str(target_id)),
            )
        if action_type in {
            "send_email_campaign",
            "send_whatsapp_campaign",
            "publish_linkedin_post",
            "publish_social_post",
        }:
            campaign_id = str(payload.get("campaign_id") or "").strip()
            if (
                not campaign_id
                and str(target_type or "").strip() == TARGET_TYPE_MARKETING_CAMPAIGN
                and target_id
            ):
                campaign_id = str(target_id)
            if campaign_id:
                family = classify_approval_family(action_type)
                return str(org_id), family, campaign_logical_key(campaign_id)
        return str(org_id), None, None
