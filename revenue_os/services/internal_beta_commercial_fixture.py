"""Bounded internal-beta commercial fixture.

Source rows stay outside git. This module pseudonymizes people, drops
phones and correspondence, and labels every written row with provenance.
It does not write to HubSpot, Clay, Gmail, or a remote CMS.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from revenue_os.models.activity import Activity, ActivityType
from revenue_os.models.contact import Company, Contact, ContactSource, ContactStatus, Industry

logger = logging.getLogger(__name__)

FIXTURE_VERSION = "internal-beta-commercial-1"


@dataclass(frozen=True)
class FixtureCounts:
    companies: int
    contacts: int
    prospects: int
    leads: int
    qualified: int
    opportunities: int
    customers: int
    activities: int
    unmapped_lifecycle: int


def provenance(*, source_system: str, source_record_id: str, source_truth: str) -> str:
    return (
        f"fixture_version={FIXTURE_VERSION}; "
        f"source_system={source_system}; "
        f"source_record_id={source_record_id}; "
        f"source_truth={source_truth}"
    )


def _tag(source_system: str, kind: str, source_record_id: str) -> str:
    return f"beta-fixture:{FIXTURE_VERSION}:{source_system}:{kind}:{source_record_id}"


def _blank(value: str | None) -> bool:
    return not (value or "").strip()


def select_hubspot_contacts(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Leads only. Opportunity and other lifecycle values stay unmapped."""
    leads = []
    for row in rows:
        stage = (row.get("Lifecycle Stage") or "").strip()
        record_id = (row.get("Record ID") or "").strip()
        if stage != "Lead" or not record_id:
            continue
        leads.append(row)
    leads.sort(key=lambda row: (row.get("Record ID") or ""))

    def take(predicate, limit: int, chosen: list[dict[str, str]]) -> None:
        seen = {(row.get("Record ID") or "") for row in chosen}
        for row in leads:
            if len([item for item in chosen if predicate(item)]) >= limit and predicate(row):
                continue
            if not predicate(row):
                continue
            record_id = row.get("Record ID") or ""
            if record_id in seen:
                continue
            chosen.append(row)
            seen.add(record_id)
            if sum(1 for item in chosen if predicate(item)) >= limit:
                return

    chosen: list[dict[str, str]] = []
    take(lambda row: not _blank(row.get("Last Activity Date")), 4, chosen)
    take(lambda row: not _blank(row.get("Associated Company")), 6, chosen)
    take(lambda row: _blank(row.get("Associated Company")), 4, chosen)
    chosen.sort(key=lambda row: row.get("Record ID") or "")
    return chosen


def unmapped_lifecycle_count(rows: list[dict[str, str]]) -> int:
    count = 0
    for row in rows:
        stage = (row.get("Lifecycle Stage") or "").strip()
        if stage and stage != "Lead":
            count += 1
    return count


def _parse_activity_time(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _activity_type(value: str) -> ActivityType | None:
    token = (value or "").strip().lower()
    if token == "email":
        return ActivityType.EMAIL
    if token == "meeting":
        return ActivityType.MEETING
    if token == "task":
        return ActivityType.TASK
    return None


def _find_tagged(db: Session, model: type, tag: str):
    return db.query(model).filter(model.tags == tag).first()


def _available_domain(db: Session, domain: str | None) -> str | None:
    if domain is None:
        return None
    taken = db.query(Company).filter(Company.domain == domain).first()
    if taken is not None:
        return None
    return domain


def _domain_or_none(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    if not text or " " in text or "." not in text:
        return None
    first = text.split(";")[0].split(",")[0].strip()
    if not first or " " in first or "." not in first:
        return None
    if first.endswith("linktr.ee"):
        return None
    return first[:255]


def seed_commercial_fixture(
    db: Session,
    *,
    organization_id: uuid.UUID,
    hubspot_companies: list[dict[str, str]],
    hubspot_contacts: list[dict[str, str]],
    clay_prospects: list[dict[str, str]],
    confirm_staging_fixture: bool,
) -> FixtureCounts:
    if not confirm_staging_fixture:
        raise ValueError("staging fixture confirmation is required")

    selected = select_hubspot_contacts(hubspot_contacts)
    companies_by_name = {}
    for row in hubspot_companies:
        name = (row.get("Company name") or "").strip()
        record_id = (row.get("Record ID") or "").strip()
        if not name or not record_id:
            continue
        companies_by_name.setdefault(name.casefold(), row)

    for clay in clay_prospects:
        source_record_id = (clay.get("source_record_id") or "").strip()
        clay_name = (clay.get("name") or "").strip()
        if not source_record_id or not clay_name:
            continue
        tag = _tag("clay", "company", source_record_id)
        existing = _find_tagged(db, Company, tag)
        notes = provenance(
            source_system="clay",
            source_record_id=source_record_id,
            source_truth="source_derived",
        )
        if existing is None:
            existing = Company(
                name=clay_name[:255],
                domain=_available_domain(db, _domain_or_none(clay.get("domain"))),
                industry=Industry.OTHER,
                tags=tag,
                notes=notes,
                organization_id=organization_id,
            )
            db.add(existing)
        else:
            existing.organization_id = organization_id
            existing.notes = notes
            existing.name = clay_name[:255]

    for row in selected:
        record_id = (row.get("Record ID") or "").strip()
        company_name = (row.get("Associated Company") or "").strip()
        company = None
        if company_name:
            source_company = companies_by_name.get(company_name.casefold())
            source_id = (source_company or {}).get("Record ID") or f"name:{company_name.casefold()}"
            tag = _tag("hubspot", "company", source_id)
            company = _find_tagged(db, Company, tag)
            notes = provenance(
                source_system="hubspot",
                source_record_id=source_id,
                source_truth="source_derived",
            )
            domain = _available_domain(db, _domain_or_none((source_company or {}).get("Additional Domains")))
            if company is None:
                company = Company(
                    name=company_name[:255],
                    domain=domain,
                    industry=Industry.OTHER,
                    tags=tag,
                    notes=notes,
                    organization_id=organization_id,
                )
                db.add(company)
                db.flush()
            else:
                company.organization_id = organization_id
                company.notes = notes

        contact_tag = _tag("hubspot", "contact", record_id)
        contact = _find_tagged(db, Contact, contact_tag)
        contact_notes = provenance(
            source_system="hubspot",
            source_record_id=record_id,
            source_truth="source_derived",
        )
        if company is not None:
            db.flush()
        if contact is None:
            contact = Contact(
                first_name="Source",
                last_name=f"Lead {record_id[-6:]}",
                email=f"hubspot-{record_id}@fixture.invalid",
                phone=None,
                company_id=company.id if company is not None else None,
                source=ContactSource.IMPORT,
                status=ContactStatus.LEAD,
                lead_score=0,
                tags=contact_tag,
                notes=contact_notes,
                organization_id=organization_id,
            )
            db.add(contact)
            db.flush()
        else:
            contact.organization_id = organization_id
            contact.notes = contact_notes
            contact.phone = None
            contact.email = f"hubspot-{record_id}@fixture.invalid"
            contact.first_name = "Source"
            contact.last_name = f"Lead {record_id[-6:]}"
            contact.status = ContactStatus.LEAD
            if company is not None:
                contact.company_id = company.id

        performed = _parse_activity_time(row.get("Last Activity Date") or "")
        kind = _activity_type(row.get("Type of first engagement") or "")
        if performed is not None and kind is not None:
            activity = (
                db.query(Activity)
                .filter(
                    Activity.contact_id == contact.id,
                    Activity.subject == "Source activity recorded. Correspondence was not imported.",
                )
                .first()
            )
            if activity is None:
                activity = Activity(
                    contact_id=contact.id,
                    activity_type=kind,
                    subject="Source activity recorded. Correspondence was not imported.",
                    body="",
                    direction="inbound",
                    status="completed",
                    performed_at=performed,
                    is_completed=1,
                    completed_at=performed,
                )
                db.add(activity)
            else:
                activity.activity_type = kind
                activity.body = ""
                activity.performed_at = performed
                activity.is_completed = 1
                activity.completed_at = performed

    db.flush()
    prefix = f"beta-fixture:{FIXTURE_VERSION}:"
    company_count = (
        db.query(Company)
        .filter(Company.organization_id == organization_id, Company.tags.like(f"{prefix}%"))
        .count()
    )
    contact_count = (
        db.query(Contact)
        .filter(Contact.organization_id == organization_id, Contact.tags.like(f"{prefix}%"))
        .count()
    )
    activity_count = (
        db.query(Activity)
        .join(Contact, Activity.contact_id == Contact.id)
        .filter(
            Contact.organization_id == organization_id,
            Activity.subject == "Source activity recorded. Correspondence was not imported.",
        )
        .count()
    )
    db.commit()
    logger.info(
        "Internal beta fixture seeded version=%s organization_id=%s contacts=%s",
        FIXTURE_VERSION,
        organization_id,
        contact_count,
    )
    prospect_count = (
        db.query(Company)
        .filter(
            Company.organization_id == organization_id,
            Company.tags.like(f"{prefix}clay:company:%"),
        )
        .count()
    )
    return FixtureCounts(
        companies=company_count,
        contacts=contact_count,
        prospects=prospect_count,
        leads=contact_count,
        qualified=0,
        opportunities=0,
        customers=0,
        activities=activity_count,
        unmapped_lifecycle=unmapped_lifecycle_count(hubspot_contacts),
    )
