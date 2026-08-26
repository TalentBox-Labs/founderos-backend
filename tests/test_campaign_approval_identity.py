"""Campaign ApprovalRequest canonical identity — tenant + channel semantics."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

import revenue_os.models  # noqa: F401
from revenue_os.models.approvals import ApprovalRequest
from revenue_os.models.base import Base
from revenue_os.models.marketing import MarketingCampaign
from revenue_os.models.organization import Organization, OrganizationStatus
from revenue_os.services.approval_request_identity import (
    FAMILY_EMAIL_CAMPAIGN,
    FAMILY_LINKEDIN_CAMPAIGN,
    FAMILY_SOCIAL_CAMPAIGN,
    FAMILY_WHATSAPP_CAMPAIGN,
    TARGET_TYPE_MARKETING_CAMPAIGN,
    campaign_logical_key,
    classify_approval_family,
    effect_key_for_approval,
)
from revenue_os.services.approvals import (
    EXECUTION_PHASE_COMPLETED,
    decide,
    get_or_create_pending_approval,
    resume_approval_effect,
)
from revenue_os.services.identity_context import AuthMethod, IdentityContext, PrincipalKind
from revenue_os.services.marketing_campaign_approval import (
    create_marketing_campaign,
    request_campaign_channel_approval,
)
from revenue_os.services.tenant_context import TenantContext

_ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_CONTACT_A = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


@pytest.fixture
def campaign_db(monkeypatch: pytest.MonkeyPatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'campaign_approval.db'}")
    Base.metadata.create_all(bind=engine)
    sf = sessionmaker(bind=engine)
    import revenue_os.database as db_mod
    import revenue_os.services.approvals as approvals_mod
    import revenue_os.services.marketing_campaign_approval as mca_mod

    monkeypatch.setattr(db_mod, "SessionLocal", sf)
    monkeypatch.setattr(approvals_mod, "SessionLocal", sf)
    monkeypatch.setattr(mca_mod, "SessionLocal", sf)

    db = sf()
    try:
        db.add_all(
            [
                Organization(
                    id=_ORG_A, name="A", slug="a", status=OrganizationStatus.ACTIVE
                ),
                Organization(
                    id=_ORG_B, name="B", slug="b", status=OrganizationStatus.ACTIVE
                ),
            ]
        )
        db.commit()
    finally:
        db.close()
    return sf


def _tenant(org_id: uuid.UUID = _ORG_A) -> TenantContext:
    identity = IdentityContext(
        principal_kind=PrincipalKind.HUMAN,
        auth_method=AuthMethod.NONE,
        is_human=True,
        user_id=str(uuid.uuid4()),
        email="owner@a.example",
        display_name="Owner",
        role="owner",
    )
    return TenantContext(
        identity=identity,
        organization_id=str(org_id),
        organization_name="Org",
        organization_slug="org",
        membership_id=str(uuid.uuid4()),
        membership_role="owner",
        membership_status="active",
    )


def test_campaign_family_mapping() -> None:
    assert classify_approval_family("send_email_campaign") == FAMILY_EMAIL_CAMPAIGN
    assert (
        classify_approval_family("send_whatsapp_campaign") == FAMILY_WHATSAPP_CAMPAIGN
    )
    assert (
        classify_approval_family("publish_linkedin_post") == FAMILY_LINKEDIN_CAMPAIGN
    )
    assert classify_approval_family("publish_social_post") == FAMILY_SOCIAL_CAMPAIGN


def test_persist_campaign_before_approval(campaign_db) -> None:
    out = request_campaign_channel_approval(
        requested_by="email_marketing_agent",
        action_type="send_email_campaign",
        title="Newsletter send",
        organization_id=str(_ORG_A),
        campaign_name="Q1 Newsletter",
        payload={
            "campaign_type": "newsletter",
            "subject": "Hello",
            "body": "Body",
            "recipients": ["a@example.com"],
        },
    )
    assert out["created"] is True
    assert out["approval_family"] == FAMILY_EMAIL_CAMPAIGN
    assert out["logical_key"] == campaign_logical_key(out["campaign_id"])
    assert out["target_type"] == TARGET_TYPE_MARKETING_CAMPAIGN
    assert out["target_id"] == out["campaign_id"]
    assert out["payload"]["campaign_id"] == out["campaign_id"]
    assert "contact_id" not in out["payload"]

    db = campaign_db()
    try:
        row = db.get(MarketingCampaign, uuid.UUID(out["campaign_id"]))
        assert row is not None
        assert str(row.organization_id) == str(_ORG_A)
        assert row.status == "planning"
        pending = (
            db.query(ApprovalRequest)
            .filter(ApprovalRequest.status == "pending")
            .count()
        )
        assert pending == 1
    finally:
        db.close()


def test_same_channel_identical_material_dedupes(campaign_db) -> None:
    first = request_campaign_channel_approval(
        requested_by="email_marketing_agent",
        action_type="send_email_campaign",
        title="Send A",
        organization_id=str(_ORG_A),
        campaign_name="Camp",
        payload={"subject": "S", "body": "B", "recipients": ["x@y.z"]},
    )
    second = request_campaign_channel_approval(
        requested_by="email_marketing_agent",
        action_type="send_email_campaign",
        title="Send A again",
        organization_id=str(_ORG_A),
        campaign_id=first["campaign_id"],
        payload={"subject": "S", "body": "B", "recipients": ["x@y.z"]},
    )
    assert second["deduplicated"] is True
    assert second["id"] == first["id"]
    assert second["created"] is False


def test_material_change_supersedes(campaign_db) -> None:
    first = request_campaign_channel_approval(
        requested_by="email_marketing_agent",
        action_type="send_email_campaign",
        title="Send A",
        organization_id=str(_ORG_A),
        campaign_name="Camp",
        payload={"subject": "S1", "body": "B1"},
    )
    second = request_campaign_channel_approval(
        requested_by="email_marketing_agent",
        action_type="send_email_campaign",
        title="Send A v2",
        organization_id=str(_ORG_A),
        campaign_id=first["campaign_id"],
        payload={"subject": "S2", "body": "B2"},
    )
    assert second["created"] is True
    assert second["id"] != first["id"]
    assert first["id"] in second["superseded_ids"]

    db = campaign_db()
    try:
        old = db.get(ApprovalRequest, first["id"])
        assert old is not None
        assert old.status == "superseded"
        pending = (
            db.query(ApprovalRequest)
            .filter(
                ApprovalRequest.status == "pending",
                ApprovalRequest.approval_family == FAMILY_EMAIL_CAMPAIGN,
            )
            .count()
        )
        assert pending == 1
    finally:
        db.close()


def test_multi_channel_same_campaign_distinct(campaign_db) -> None:
    email = request_campaign_channel_approval(
        requested_by="email_marketing_agent",
        action_type="send_email_campaign",
        title="Email",
        organization_id=str(_ORG_A),
        campaign_name="Multi",
        channels=["email", "whatsapp"],
        payload={"subject": "S", "body": "B"},
    )
    wa = request_campaign_channel_approval(
        requested_by="whatsapp_marketing_agent",
        action_type="send_whatsapp_campaign",
        title="WhatsApp",
        organization_id=str(_ORG_A),
        campaign_id=email["campaign_id"],
        payload={"message": "Hi", "tag": "vip"},
    )
    assert email["campaign_id"] == wa["campaign_id"]
    assert email["id"] != wa["id"]
    assert email["approval_family"] == FAMILY_EMAIL_CAMPAIGN
    assert wa["approval_family"] == FAMILY_WHATSAPP_CAMPAIGN
    assert email["logical_key"] == wa["logical_key"] == campaign_logical_key(
        email["campaign_id"]
    )


def test_different_campaign_ids_distinct(campaign_db) -> None:
    a = request_campaign_channel_approval(
        requested_by="email_marketing_agent",
        action_type="send_email_campaign",
        title="A",
        organization_id=str(_ORG_A),
        campaign_name="A",
        payload={"subject": "S", "body": "B"},
    )
    b = request_campaign_channel_approval(
        requested_by="email_marketing_agent",
        action_type="send_email_campaign",
        title="B",
        organization_id=str(_ORG_A),
        campaign_name="B",
        payload={"subject": "S", "body": "B"},
    )
    assert a["campaign_id"] != b["campaign_id"]
    assert a["id"] != b["id"]


def test_terminal_approval_not_silently_reused(campaign_db, monkeypatch) -> None:
    monkeypatch.setattr(
        "revenue_os.services.approvals.EXECUTORS",
        {
            "send_email_campaign": lambda _db, _p: {
                "executed": True,
                "note": "test stub",
            }
        },
    )
    created = request_campaign_channel_approval(
        requested_by="email_marketing_agent",
        action_type="send_email_campaign",
        title="Send",
        organization_id=str(_ORG_A),
        campaign_name="Once",
        payload={"subject": "S", "body": "B"},
    )
    decided = decide(created["id"], True, tenant=_tenant(_ORG_A))
    assert decided["status"] == "approved"

    # Same campaign_id after terminal — new pending allowed (not silent reuse of terminal).
    again = request_campaign_channel_approval(
        requested_by="email_marketing_agent",
        action_type="send_email_campaign",
        title="Send again",
        organization_id=str(_ORG_A),
        campaign_id=created["campaign_id"],
        payload={"subject": "S", "body": "B"},
    )
    assert again["created"] is True
    assert again["id"] != created["id"]
    assert again["status"] == "pending"


def test_cross_tenant_same_logical_key_no_collision(campaign_db) -> None:
    """Identical logical_key tokens must not collide across organization_id."""
    shared_key = campaign_logical_key("00000000-0000-4000-8000-000000000099")
    camp_a = create_marketing_campaign(
        organization_id=str(_ORG_A), name="A-camp", channels=["email"]
    )
    camp_b = create_marketing_campaign(
        organization_id=str(_ORG_B), name="B-camp", channels=["email"]
    )
    a = get_or_create_pending_approval(
        requested_by="agent",
        action_type="send_email_campaign",
        title="A",
        organization_id=str(_ORG_A),
        target_type=TARGET_TYPE_MARKETING_CAMPAIGN,
        target_id=camp_a["id"],
        payload={
            "organization_id": str(_ORG_A),
            "campaign_id": camp_a["id"],
            "subject": "S",
            "body": "B",
        },
    )
    b = get_or_create_pending_approval(
        requested_by="agent",
        action_type="send_email_campaign",
        title="B",
        organization_id=str(_ORG_B),
        target_type=TARGET_TYPE_MARKETING_CAMPAIGN,
        target_id=camp_b["id"],
        payload={
            "organization_id": str(_ORG_B),
            "campaign_id": camp_b["id"],
            "subject": "S",
            "body": "B",
        },
    )
    assert a["id"] != b["id"]
    assert a["organization_id"] == str(_ORG_A)
    assert b["organization_id"] == str(_ORG_B)

    # Direct row insert with identical logical_key across orgs must both remain pending.
    db = campaign_db()
    try:
        db.add(
            ApprovalRequest(
                organization_id=str(_ORG_A),
                approval_family=FAMILY_EMAIL_CAMPAIGN,
                logical_key=shared_key,
                requested_by="seed",
                action_type="send_email_campaign",
                title="shared-a",
                payload={"organization_id": str(_ORG_A), "campaign_id": "x"},
                status="pending",
            )
        )
        db.add(
            ApprovalRequest(
                organization_id=str(_ORG_B),
                approval_family=FAMILY_EMAIL_CAMPAIGN,
                logical_key=shared_key,
                requested_by="seed",
                action_type="send_email_campaign",
                title="shared-b",
                payload={"organization_id": str(_ORG_B), "campaign_id": "x"},
                status="pending",
            )
        )
        db.commit()
        twins = (
            db.query(ApprovalRequest)
            .filter(
                ApprovalRequest.logical_key == shared_key,
                ApprovalRequest.status == "pending",
            )
            .count()
        )
        assert twins == 2
        total_pending = (
            db.query(func.count(ApprovalRequest.id))
            .filter(ApprovalRequest.status == "pending")
            .scalar()
        )
        assert total_pending == 4
    finally:
        db.close()


def test_org_a_campaign_not_resolvable_from_org_b(campaign_db) -> None:
    camp = create_marketing_campaign(
        organization_id=str(_ORG_A), name="A only", channels=["email"]
    )
    with pytest.raises(ValueError, match="not in tenant scope"):
        request_campaign_channel_approval(
            requested_by="agent",
            action_type="send_email_campaign",
            title="Steal",
            organization_id=str(_ORG_B),
            campaign_id=camp["id"],
            payload={"subject": "S", "body": "B"},
        )


def test_org_a_approval_not_decidable_from_org_b(campaign_db, monkeypatch) -> None:
    monkeypatch.setattr(
        "revenue_os.services.approvals.EXECUTORS",
        {"send_email_campaign": lambda _db, _p: {"executed": True}},
    )
    created = request_campaign_channel_approval(
        requested_by="agent",
        action_type="send_email_campaign",
        title="A",
        organization_id=str(_ORG_A),
        campaign_name="A",
        payload={"subject": "S", "body": "B"},
    )
    with pytest.raises(ValueError, match="not found"):
        decide(created["id"], True, tenant=_tenant(_ORG_B))


def test_missing_organization_fails_closed(campaign_db) -> None:
    camp = create_marketing_campaign(
        organization_id=str(_ORG_A), name="Needs org", channels=["email"]
    )
    with pytest.raises(ValueError, match="organization_id"):
        get_or_create_pending_approval(
            requested_by="agent",
            action_type="send_email_campaign",
            title="No org",
            target_type=TARGET_TYPE_MARKETING_CAMPAIGN,
            target_id=camp["id"],
            payload={"campaign_id": camp["id"], "subject": "S"},
            organization_id=None,
        )


def test_missing_campaign_id_cannot_fall_back_to_contact_id(campaign_db) -> None:
    with pytest.raises(ValueError, match="campaign_id"):
        get_or_create_pending_approval(
            requested_by="agent",
            action_type="send_email_campaign",
            title="No campaign",
            organization_id=str(_ORG_A),
            target_type="contact",
            target_id=str(_CONTACT_A),
            payload={
                "organization_id": str(_ORG_A),
                "contact_id": str(_CONTACT_A),
                "subject": "S",
            },
        )


def test_campaign_identity_never_fabricated(campaign_db) -> None:
    fake_id = str(uuid.uuid4())
    with pytest.raises(ValueError, match="not in tenant scope|campaign_id"):
        get_or_create_pending_approval(
            requested_by="agent",
            action_type="send_email_campaign",
            title="Fabricated",
            organization_id=str(_ORG_A),
            target_type=TARGET_TYPE_MARKETING_CAMPAIGN,
            target_id=fake_id,
            payload={
                "organization_id": str(_ORG_A),
                "campaign_id": fake_id,
                "subject": "S",
            },
        )
    db = campaign_db()
    try:
        assert db.query(ApprovalRequest).count() == 0
        assert db.query(MarketingCampaign).count() == 0
    finally:
        db.close()


def test_linkedin_and_social_families(campaign_db) -> None:
    linkedin = request_campaign_channel_approval(
        requested_by="content_agent",
        action_type="publish_linkedin_post",
        title="LI",
        organization_id=str(_ORG_A),
        campaign_name="Social pack",
        payload={"body": "Post body"},
    )
    social = request_campaign_channel_approval(
        requested_by="content_agent",
        action_type="publish_social_post",
        title="Social",
        organization_id=str(_ORG_A),
        campaign_id=linkedin["campaign_id"],
        payload={"variants": [{"platform": "x", "text": "t"}]},
    )
    assert linkedin["approval_family"] == FAMILY_LINKEDIN_CAMPAIGN
    assert social["approval_family"] == FAMILY_SOCIAL_CAMPAIGN
    assert linkedin["id"] != social["id"]


def test_internal_effect_key_stable_on_campaign_approve(
    campaign_db, monkeypatch
) -> None:
    monkeypatch.setattr(
        "revenue_os.services.approvals.EXECUTORS",
        {
            "send_email_campaign": lambda _db, _p: {
                "executed": True,
                "handed_to_n8n": True,
            }
        },
    )
    created = request_campaign_channel_approval(
        requested_by="agent",
        action_type="send_email_campaign",
        title="Send",
        organization_id=str(_ORG_A),
        campaign_name="FX",
        payload={"subject": "S", "body": "B"},
    )
    expected = effect_key_for_approval(created["id"], "send_email_campaign")
    out = decide(created["id"], True, tenant=_tenant(_ORG_A))
    assert out["execution_result"]["effect_key"] == expected
    assert out["execution_result"]["phase"] == EXECUTION_PHASE_COMPLETED

    resumed = resume_approval_effect(created["id"], tenant=_tenant(_ORG_A))
    assert resumed["execution_result"]["effect_key"] == expected
    assert resumed["execution_result"]["phase"] == EXECUTION_PHASE_COMPLETED
