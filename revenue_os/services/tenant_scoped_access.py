"""SaaS S2 — bounded tenant-scoped object resolution (organization_id + object_id)."""

from __future__ import annotations

import uuid as uuid_lib
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from revenue_os.models.automation_state import AgentActionLog
from revenue_os.models.contact import Contact
from revenue_os.models.deal import Deal
from revenue_os.services.commercial_outcome_service import ACTION_HANDOFF as CO_HANDOFF
from revenue_os.services.qualified_demand_service import ACTION_HANDOFF as QD_HANDOFF


class TenantAccessError(ValueError):
    """Raised when an object is absent or not owned by the tenant."""


def tenant_org_uuid(tenant_org_id: str | None) -> uuid_lib.UUID | None:
    if tenant_org_id is None:
        return None
    try:
        return uuid_lib.UUID(str(tenant_org_id))
    except ValueError:
        return None


def apply_contact_org_filter(query, organization_id: str | None):  # noqa: ANN001
    org_uuid = tenant_org_uuid(organization_id)
    if org_uuid is not None:
        return query.filter(Contact.organization_id == org_uuid)
    return query


def apply_deal_org_filter(query, organization_id: str | None):  # noqa: ANN001
    org_uuid = tenant_org_uuid(organization_id)
    if org_uuid is not None:
        return query.filter(Deal.organization_id == org_uuid)
    return query


def stamp_new_contact_org(contact: Contact, organization_id: str | None) -> None:
    org_uuid = tenant_org_uuid(organization_id)
    if org_uuid is not None:
        contact.organization_id = org_uuid


def stamp_new_deal_org(deal: Deal, organization_id: str | None) -> None:
    org_uuid = tenant_org_uuid(organization_id)
    if org_uuid is not None:
        deal.organization_id = org_uuid


def verify_activity_in_tenant(
    db: Session, organization_id: str | None, activity: Any
) -> bool:
    """True when activity is visible in tenant scope (via linked contact or deal)."""
    if organization_id is None:
        return True
    if activity.contact_id is not None:
        contact = db.get(Contact, activity.contact_id)
        if contact is not None and _org_match(contact.organization_id, organization_id):
            return True
    if activity.deal_id is not None:
        deal = db.get(Deal, activity.deal_id)
        if deal is not None and _org_match(deal.organization_id, organization_id):
            return True
    return False


def _org_match(record_org_id: Any, tenant_org_id: str) -> bool:
    if record_org_id is None:
        return False
    return str(record_org_id) == str(tenant_org_id)


def _http_from_access(exc: TenantAccessError, *, kind: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{kind} not found")


def get_contact_for_tenant(
    db: Session, organization_id: str, contact_id: str
) -> Contact:
    try:
        cid = uuid_lib.UUID(contact_id.strip())
    except ValueError as exc:
        raise TenantAccessError("Invalid contact_id") from exc
    contact = db.get(Contact, cid)
    if contact is None or not _org_match(contact.organization_id, organization_id):
        raise TenantAccessError("Contact not in tenant scope")
    return contact


def get_deal_for_tenant(db: Session, organization_id: str, deal_id: str) -> Deal:
    try:
        did = uuid_lib.UUID(deal_id.strip())
    except ValueError as exc:
        raise TenantAccessError("Invalid deal_id") from exc
    deal = db.get(Deal, did)
    if deal is None or not _org_match(deal.organization_id, organization_id):
        raise TenantAccessError("Deal not in tenant scope")
    return deal


def get_qualified_demand_handoff_for_tenant(
    db: Session, organization_id: str, demand_id: str
) -> AgentActionLog:
    row = (
        db.query(AgentActionLog)
        .filter(
            AgentActionLog.action_type == QD_HANDOFF,
            AgentActionLog.target_id == demand_id.strip(),
        )
        .first()
    )
    if row is None or not _org_match(row.organization_id, organization_id):
        raise TenantAccessError("QualifiedDemand handoff not in tenant scope")
    return row


def get_commercial_outcome_handoff_for_tenant(
    db: Session, organization_id: str, outcome_id: str
) -> AgentActionLog:
    row = (
        db.query(AgentActionLog)
        .filter(
            AgentActionLog.action_type == CO_HANDOFF,
            AgentActionLog.target_id == outcome_id.strip(),
        )
        .first()
    )
    if row is None or not _org_match(row.organization_id, organization_id):
        raise TenantAccessError("CommercialOutcome handoff not in tenant scope")
    return row


def stamp_agent_action_log_organization(
    db: Session, *, action_type: str, target_id: str, organization_id: str
) -> None:
    row = (
        db.query(AgentActionLog)
        .filter(
            AgentActionLog.action_type == action_type,
            AgentActionLog.target_id == target_id,
        )
        .order_by(AgentActionLog.created_at.desc())
        .first()
    )
    if row is not None and row.organization_id is None:
        row.organization_id = uuid_lib.UUID(str(organization_id))
        db.add(row)
        db.commit()


def get_marketing_campaign_for_tenant(
    db: Session, organization_id: str, campaign_id: str
) -> Any:
    """Resolve MarketingCampaign only when owned by the tenant organization."""
    from revenue_os.models.marketing import MarketingCampaign

    try:
        cid = uuid_lib.UUID(str(campaign_id).strip())
    except ValueError as exc:
        raise TenantAccessError("Invalid campaign_id") from exc
    campaign = db.get(MarketingCampaign, cid)
    if campaign is None or not _org_match(campaign.organization_id, organization_id):
        raise TenantAccessError("MarketingCampaign not in tenant scope")
    return campaign


def get_approval_for_tenant(
    db: Session, organization_id: str, request_id: str
) -> Any:
    """Resolve ApprovalRequest only when tenant-bound via payload or contact."""
    from revenue_os.models.approvals import ApprovalRequest

    row = db.get(ApprovalRequest, request_id.strip())
    if row is None:
        raise TenantAccessError("ApprovalRequest not in tenant scope")

    if row.organization_id is not None:
        if not _org_match(row.organization_id, organization_id):
            raise TenantAccessError("ApprovalRequest not in tenant scope")
    else:
        payload = row.payload or {}
        org_in_payload = payload.get("organization_id")
        if org_in_payload is not None:
            if not _org_match(org_in_payload, organization_id):
                raise TenantAccessError("ApprovalRequest not in tenant scope")
        elif row.target_type == "contact" and row.target_id:
            get_contact_for_tenant(db, organization_id, str(row.target_id))
        else:
            raise TenantAccessError("ApprovalRequest not in tenant scope")

    return row


def stamp_contact_organization_if_missing(
    db: Session, contact_id: str, organization_id: str
) -> None:
    try:
        cid = uuid_lib.UUID(contact_id)
    except ValueError:
        return
    contact = db.get(Contact, cid)
    if contact is not None and contact.organization_id is None:
        contact.organization_id = uuid_lib.UUID(str(organization_id))
        db.add(contact)
        db.commit()
