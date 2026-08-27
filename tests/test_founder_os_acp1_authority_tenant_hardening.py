"""ACP-1 — Authority & tenant hardening for autonomous commercial paths."""

from __future__ import annotations

import inspect
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import revenue_os.models  # noqa: F401
from revenue_os.models.automation_state import AgentActionLog
from revenue_os.models.base import Base
from revenue_os.models.contact import Contact, ContactSource, ContactStatus
from revenue_os.models.deal import Deal, Pipeline, PipelineType
from revenue_os.models.organization import Organization, OrganizationStatus
from revenue_os.services import acp1_autonomous_boundary as boundary
from revenue_os.services.approvals import decide, request_approval
from revenue_os.services.command_operating_surface import (
    EXEC_INLINE_GOVERNED,
    actions_for_decision_item,
)
from revenue_os.services.deal_automation_service import create_deal_from_contact
from revenue_os.services.hermes_planner import (
    action_create_deals_for_qualified,
    action_score_unscored_leads,
    check_all_active_goals,
)
from revenue_os.services.identity_context import AuthMethod, IdentityContext, PrincipalKind
from revenue_os.services.tenant_context import TenantContext

_ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_CONTACT_A = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_CONTACT_B = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def tenant_db(monkeypatch: pytest.MonkeyPatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'acp1.db'}")
    Base.metadata.create_all(bind=engine)
    sf = sessionmaker(bind=engine)
    import revenue_os.database as db_mod
    import revenue_os.integrations.gmail_sync as gmail_mod
    import revenue_os.scheduler as sched
    import revenue_os.services.activity_log as al
    import revenue_os.services.approvals as approvals_mod
    import revenue_os.services.hermes_planner as hermes
    import revenue_os.services.revenue_orchestration_service as ros

    for mod in (db_mod, al, sched, hermes, approvals_mod, gmail_mod, ros):
        if hasattr(mod, "SessionLocal"):
            monkeypatch.setattr(mod, "SessionLocal", sf)
    monkeypatch.delenv("ACP1_AUTONOMOUS_ORGANIZATION_IDS", raising=False)
    monkeypatch.delenv("HEARTBEAT_ORGANIZATION_IDS", raising=False)
    return sf


def _seed(sf: sessionmaker, *, with_orgs: bool = True) -> None:
    db = sf()
    try:
        if with_orgs:
            db.add_all(
                [
                    Organization(
                        id=_ORG_A, name="Org A", slug="org-a", status=OrganizationStatus.ACTIVE
                    ),
                    Organization(
                        id=_ORG_B, name="Org B", slug="org-b", status=OrganizationStatus.ACTIVE
                    ),
                ]
            )
        pipeline = Pipeline(name="Sales", pipeline_type=PipelineType.SALES, is_default=1)
        db.add(pipeline)
        db.flush()
        db.add_all(
            [
                Contact(
                    id=_CONTACT_A,
                    first_name="Ada",
                    last_name="A",
                    email="ada@a.example",
                    status=ContactStatus.LEAD,
                    source=ContactSource.MANUAL,
                    organization_id=_ORG_A,
                    lead_score=0,
                ),
                Contact(
                    id=_CONTACT_B,
                    first_name="Bob",
                    last_name="B",
                    email="bob@b.example",
                    status=ContactStatus.LEAD,
                    source=ContactSource.MANUAL,
                    organization_id=_ORG_B,
                    lead_score=0,
                ),
            ]
        )
        db.commit()
    finally:
        db.close()


# ── Tenant enumeration ───────────────────────────────────────────────────────


def test_enumeration_uses_active_organizations_not_contacts(tenant_db, monkeypatch):
    _seed(tenant_db)
    db = tenant_db()
    try:
        # Orphan contact org id that is NOT an Organization row must not authorize.
        orphan = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
        db.add(
            Contact(
                first_name="Orphan",
                last_name="X",
                email="orphan@example.com",
                status=ContactStatus.LEAD,
                source=ContactSource.MANUAL,
                organization_id=orphan,
                lead_score=0,
            )
        )
        db.commit()
        orgs = boundary.resolve_autonomous_organization_ids(db)
        assert str(_ORG_A) in orgs
        assert str(_ORG_B) in orgs
        assert str(orphan) not in orgs
    finally:
        db.close()


def test_empty_allowlist_fails_closed(tenant_db, monkeypatch):
    _seed(tenant_db)
    monkeypatch.setenv("ACP1_AUTONOMOUS_ORGANIZATION_IDS", "")
    db = tenant_db()
    try:
        assert boundary.resolve_autonomous_organization_ids(db) == []
    finally:
        db.close()


def test_allowlist_intersected_with_active_org(tenant_db, monkeypatch):
    _seed(tenant_db)
    monkeypatch.setenv("ACP1_AUTONOMOUS_ORGANIZATION_IDS", str(_ORG_A))
    db = tenant_db()
    try:
        assert boundary.resolve_autonomous_organization_ids(db) == [str(_ORG_A)]
    finally:
        db.close()


def test_no_organizations_fail_closed_scoring(tenant_db, monkeypatch):
    """Missing canonical tenants → commercial heartbeat fails closed."""
    from revenue_os.scheduler import job_score_new_leads

    # Contact without Organization rows
    db = tenant_db()
    try:
        db.add(
            Contact(
                id=_CONTACT_A,
                first_name="Ada",
                last_name="A",
                email="ada@a.example",
                status=ContactStatus.LEAD,
                source=ContactSource.MANUAL,
                organization_id=_ORG_A,
                lead_score=0,
            )
        )
        db.commit()
    finally:
        db.close()

    result = job_score_new_leads()
    assert result.get("blocked") is True
    assert result.get("blocked_reason") == boundary.BLOCKED_MISSING_TENANT


# ── Cross-tenant isolation ───────────────────────────────────────────────────


def test_score_job_does_not_cross_tenants(tenant_db, monkeypatch):
    from revenue_os.scheduler import job_score_new_leads
    import revenue_os.services.lead_scoring_service as scoring

    def _fake_score(db, contact, company_context=None):
        contact.lead_score = 55
        db.add(contact)
        return {"score": 55, "old_score": 0, "status_changed": False, "status": contact.status.value}

    monkeypatch.setattr(scoring, "score_contact", _fake_score)
    _seed(tenant_db)
    monkeypatch.setenv("ACP1_AUTONOMOUS_ORGANIZATION_IDS", str(_ORG_A))
    result = job_score_new_leads()
    assert result.get("ok") is True
    assert result["contacts_scored"] >= 1

    db = tenant_db()
    try:
        a = db.get(Contact, _CONTACT_A)
        b = db.get(Contact, _CONTACT_B)
        assert a is not None and (a.lead_score or 0) > 0
        assert b is not None and (b.lead_score or 0) == 0
        logs = (
            db.query(AgentActionLog)
            .filter(AgentActionLog.action_type == "lead_scored")
            .all()
        )
        assert logs
        assert all(str(log.organization_id) == str(_ORG_A) for log in logs)
    finally:
        db.close()


def test_repeat_score_is_deterministic(tenant_db, monkeypatch):
    from revenue_os.scheduler import job_score_new_leads
    import revenue_os.services.lead_scoring_service as scoring

    def _fake_score(db, contact, company_context=None):
        contact.lead_score = 40
        db.add(contact)
        return {"score": 40, "old_score": 0, "status_changed": False, "status": contact.status.value}

    monkeypatch.setattr(scoring, "score_contact", _fake_score)
    _seed(tenant_db)
    monkeypatch.setenv("ACP1_AUTONOMOUS_ORGANIZATION_IDS", str(_ORG_A))
    first = job_score_new_leads()
    second = job_score_new_leads()
    assert first.get("ok") and second.get("ok")
    # After first score, contact no longer unscored → second scores 0
    assert first["contacts_scored"] >= 1
    assert second["contacts_scored"] == 0


def test_follow_up_scan_requires_organization_id(tenant_db):
    from revenue_os.services.follow_up_eligibility import scan_eligible_follow_ups

    _seed(tenant_db)
    db = tenant_db()
    try:
        with pytest.raises(TypeError):
            scan_eligible_follow_ups(db)  # type: ignore[call-arg]
        scoped = scan_eligible_follow_ups(db, organization_id=str(_ORG_A))
        assert all(item["organization_id"] == str(_ORG_A) for item in scoped)
    finally:
        db.close()


def test_hermes_deal_creation_blocked_even_with_org(tenant_db):
    _seed(tenant_db)
    db = tenant_db()
    try:
        before = db.query(Deal).count()
        result = action_create_deals_for_qualified(
            db, {"organization_id": str(_ORG_A), "limit": 10}
        )
        assert result.get("blocked") is True
        assert result.get("blocked_reason") == boundary.BLOCKED_HERMES_DEAL
        assert db.query(Deal).count() == before
        blocked_logs = (
            db.query(AgentActionLog)
            .filter(AgentActionLog.action_type == "hermes_deal_create_blocked")
            .all()
        )
        assert blocked_logs
    finally:
        db.close()


def test_hermes_score_without_org_fails_closed(tenant_db):
    _seed(tenant_db)
    db = tenant_db()
    try:
        result = action_score_unscored_leads(db, {})
        assert result.get("blocked") is True
        a = db.get(Contact, _CONTACT_A)
        assert a is not None and (a.lead_score or 0) == 0
    finally:
        db.close()


def test_check_all_active_goals_without_orgs_fails_closed(tenant_db):
    result = check_all_active_goals(organization_ids=[])
    assert result.get("blocked") is True


def test_create_deal_from_contact_stamps_org_without_widening(tenant_db):
    _seed(tenant_db)
    db = tenant_db()
    try:
        contact = db.get(Contact, _CONTACT_A)
        assert contact is not None
        contact.status = ContactStatus.QUALIFIED
        db.commit()
        deal = create_deal_from_contact(db, contact, value=1000.0)
        assert deal is not None
        assert deal.organization_id == _ORG_A
        assert not boundary.hermes_action_allowed("create_deals_for_qualified")
    finally:
        db.close()


def test_agent_cannot_approve_own_request(tenant_db):
    _seed(tenant_db)
    approval = request_approval(
        requested_by="hermes",
        action_type="send_outreach_email",
        title="Test send",
        target_type="contact",
        target_id=str(_CONTACT_A),
        payload={
            "contact_id": str(_CONTACT_A),
            "organization_id": str(_ORG_A),
            "email": "ada@a.example",
        },
        organization_id=str(_ORG_A),
    )
    agent_tenant = TenantContext(
        identity=IdentityContext(
            principal_kind=PrincipalKind.AGENT,
            auth_method=AuthMethod.API_KEY,
            is_human=False,
            user_id=None,
            email=None,
            display_name="hermes",
            role="agent",
        ),
        organization_id=str(_ORG_A),
        organization_name="Org A",
        organization_slug="org-a",
        membership_id="m-agent",
        membership_role="agent",
        membership_status="active",
    )
    from revenue_os.services.mutation_authority import HumanAuthorityError

    with pytest.raises(HumanAuthorityError):
        decide(approval["id"], approve=True, tenant=agent_tenant)


def test_cos5_inline_semantics_preserved():
    item = {
        "kind": "qualified_demand",
        "href": "/demand",
        "provenance": {"subject_id": "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
    }
    actions = actions_for_decision_item(item)
    assert actions
    assert all(a["execution_mode"] == EXEC_INLINE_GOVERNED for a in actions[:2])


def test_optional_tenant_not_imported_by_autonomous_modules():
    sched_src = (_ROOT / "revenue_os/scheduler.py").read_text()
    hermes_src = (_ROOT / "revenue_os/services/hermes_planner.py").read_text()
    boundary_src = (_ROOT / "revenue_os/services/acp1_autonomous_boundary.py").read_text()
    for src in (sched_src, hermes_src, boundary_src):
        assert "optional_tenant_mutation" not in src


def test_no_new_outbound_or_booking_in_acp1_surfaces():
    sched_src = (_ROOT / "revenue_os/scheduler.py").read_text()
    hermes_src = (_ROOT / "revenue_os/services/hermes_planner.py").read_text()
    assert "trigger_workflow" not in sched_src
    assert "send_outreach_email" not in hermes_src
    assert "_execute_book_meeting" not in hermes_src
    assert "create_deal_from_contact" not in hermes_src


def test_gmail_sync_requires_organization_ids(tenant_db):
    from revenue_os.integrations.gmail_sync import sync_inbox

    result = sync_inbox(organization_ids=[])
    assert result.get("blocked") is True
    assert sync_inbox(organization_id=None).get("blocked") is True


def test_propose_cannot_call_decide_via_hermes_registry():
    from revenue_os.services.hermes_planner import ACTION_REGISTRY

    assert "send_outreach_email" not in ACTION_REGISTRY
    assert "book_meeting" not in ACTION_REGISTRY
    assert ACTION_REGISTRY["create_deals_for_qualified"] is action_create_deals_for_qualified


def test_blocked_work_leaves_provenance(tenant_db):
    from revenue_os.scheduler import job_score_new_leads

    result = job_score_new_leads()
    assert result.get("blocked")
    db = tenant_db()
    try:
        logs = (
            db.query(AgentActionLog)
            .filter(AgentActionLog.status == "blocked")
            .all()
        )
        assert logs
    finally:
        db.close()


def test_no_new_models_or_migrations_in_acp1():
    boundary_src = inspect.getsource(boundary)
    assert "class AgentRun" not in boundary_src
    assert "alembic" not in boundary_src.lower()
