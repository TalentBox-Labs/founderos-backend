"""Canonical marketing campaign instance + channel ApprovalRequest wiring.

Live Main draft_* callers are not present in this Founder OS lineage.
This module is the local SoT for: persist MarketingCampaign, then file a
channel ApprovalRequest bound to campaign_id — without fabricating contact_id.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from revenue_os.database import SessionLocal
from revenue_os.models.marketing import MarketingCampaign
from revenue_os.services.approval_request_identity import (
    CAMPAIGN_FAMILIES,
    TARGET_TYPE_MARKETING_CAMPAIGN,
    classify_approval_family,
)
from revenue_os.services.approvals import get_or_create_pending_approval
from revenue_os.services.tenant_scoped_access import (
    TenantAccessError,
    get_marketing_campaign_for_tenant,
    tenant_org_uuid,
)

CAMPAIGN_CHANNEL_ACTIONS = frozenset(
    {
        "send_email_campaign",
        "send_whatsapp_campaign",
        "publish_linkedin_post",
        "publish_social_post",
    }
)

_CHANNEL_LABEL = {
    "send_email_campaign": "email",
    "send_whatsapp_campaign": "whatsapp",
    "publish_linkedin_post": "linkedin",
    "publish_social_post": "social",
}


def _campaign_dict(campaign: MarketingCampaign) -> dict[str, Any]:
    return {
        "id": str(campaign.id),
        "organization_id": str(campaign.organization_id) if campaign.organization_id else None,
        "name": campaign.name,
        "goal": campaign.goal or "",
        "channels": campaign.channels or "",
        "status": campaign.status,
        "kpis": campaign.kpis or "",
        "owner_agents": campaign.owner_agents or "",
        "notes": campaign.notes or "",
    }


def create_marketing_campaign(
    *,
    organization_id: str,
    name: str,
    goal: str = "",
    channels: list[str] | None = None,
    status: str = "planning",
    notes: str = "",
    db: Session | None = None,
) -> dict[str, Any]:
    """Persist a MarketingCampaign row (pre-approval planning state)."""
    org_uuid = tenant_org_uuid(organization_id)
    if org_uuid is None:
        raise ValueError("MarketingCampaign requires a valid organization_id")
    owns = db is None
    session = db or SessionLocal()
    try:
        campaign = MarketingCampaign(
            organization_id=org_uuid,
            name=name,
            goal=goal or "",
            channels=",".join(channels or []),
            status=status or "planning",
            notes=notes or "",
        )
        session.add(campaign)
        if owns:
            session.commit()
            session.refresh(campaign)
        else:
            session.flush()
            session.refresh(campaign)
        return _campaign_dict(campaign)
    finally:
        if owns:
            session.close()


def resolve_marketing_campaign(
    *,
    organization_id: str,
    campaign_id: str,
    db: Session | None = None,
) -> dict[str, Any]:
    """Resolve an existing campaign in tenant scope — fails closed cross-org."""
    owns = db is None
    session = db or SessionLocal()
    try:
        campaign = get_marketing_campaign_for_tenant(
            session, organization_id, campaign_id
        )
        return _campaign_dict(campaign)
    except TenantAccessError as exc:
        raise ValueError(str(exc)) from exc
    finally:
        if owns:
            session.close()


def ensure_marketing_campaign(
    *,
    organization_id: str,
    campaign_id: str | None = None,
    name: str | None = None,
    goal: str = "",
    channels: list[str] | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    """Resolve campaign_id when given; otherwise create a planning instance."""
    if campaign_id:
        return resolve_marketing_campaign(
            organization_id=organization_id,
            campaign_id=campaign_id,
            db=db,
        )
    if not name or not str(name).strip():
        raise ValueError(
            "Creating a MarketingCampaign requires name when campaign_id is absent"
        )
    return create_marketing_campaign(
        organization_id=organization_id,
        name=str(name).strip(),
        goal=goal,
        channels=channels,
        db=db,
    )


def request_campaign_channel_approval(
    *,
    requested_by: str,
    action_type: str,
    title: str,
    organization_id: str,
    description: str = "",
    campaign_id: str | None = None,
    campaign_name: str | None = None,
    goal: str = "",
    channels: list[str] | None = None,
    payload: dict[str, Any] | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    """Persist/resolve MarketingCampaign, then create channel ApprovalRequest.

    Does not approve, execute, or broaden authority.
    """
    if action_type not in CAMPAIGN_CHANNEL_ACTIONS:
        raise ValueError(f"Unsupported campaign channel action_type: {action_type}")
    if classify_approval_family(action_type) not in CAMPAIGN_FAMILIES:
        raise ValueError(f"Action type is not a campaign family: {action_type}")
    if not str(organization_id or "").strip():
        raise ValueError("Campaign approval requires organization_id")

    owns = db is None
    session = db or SessionLocal()
    try:
        channel = _CHANNEL_LABEL[action_type]
        default_name = campaign_name or f"Campaign ({channel})"
        campaign = ensure_marketing_campaign(
            organization_id=organization_id,
            campaign_id=campaign_id,
            name=default_name,
            goal=goal,
            channels=channels or [channel],
            db=session,
        )
        cid = str(campaign["id"])
        merged = dict(payload or {})
        merged["organization_id"] = str(organization_id)
        merged["campaign_id"] = cid
        # Never invent contact_id for broadcast campaign approvals.
        merged.pop("contact_id", None)

        approval = get_or_create_pending_approval(
            requested_by=requested_by,
            action_type=action_type,
            title=title,
            description=description,
            target_type=TARGET_TYPE_MARKETING_CAMPAIGN,
            target_id=cid,
            payload=merged,
            organization_id=organization_id,
            db=session,
        )
        if owns:
            session.commit()
        return {
            **approval,
            "campaign_id": cid,
            "campaign": campaign,
        }
    finally:
        if owns:
            session.close()


def require_campaign_bound_to_org(
    db: Session, *, organization_id: str, campaign_id: str
) -> MarketingCampaign:
    """Fail closed when campaign_id is missing, invalid, or cross-tenant."""
    if not str(organization_id or "").strip():
        raise ValueError("Campaign approval requires organization_id")
    if not str(campaign_id or "").strip():
        raise ValueError("Campaign approval requires campaign_id")
    try:
        return get_marketing_campaign_for_tenant(db, organization_id, campaign_id)
    except TenantAccessError as exc:
        raise ValueError(str(exc)) from exc


def new_campaign_id() -> str:
    """Explicit UUID helper for tests — not used as approval identity alone."""
    return str(uuid.uuid4())
