"""Member provisioning and the bounded commercial fixture."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import revenue_os.models  # noqa: F401
import revenue_os.services.tenant_resolution as tenant_resolution_mod
import runner_api_routers.identity as identity_mod
from revenue_os.auth import verify_password
from revenue_os.models.base import Base
from revenue_os.models.contact import Company, Contact
from revenue_os.models.organization import (
    MembershipStatus,
    Organization,
    OrganizationMembership,
    OrganizationStatus,
)
from revenue_os.models.user import User
from revenue_os.services.human_password_recovery import ENV_RECOVERY_SECRET
from revenue_os.services.internal_beta_commercial_fixture import (
    seed_commercial_fixture,
    select_hubspot_contacts,
)
from revenue_os.services.staging_member_provision import (
    MEMBER_ROLE,
    ProvisionOutcome,
    provision_member,
)
from src.ui.enterprise.compose import _tenant_companies

_SECRET = "test-recovery-secret-not-for-production-use-xx"
_PASSWORD = "staging-member-passphrase"
_ORG = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_OTHER = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


@pytest.fixture()
def factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sessionmaker:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'members.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    made = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setenv(ENV_RECOVERY_SECRET, _SECRET)
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-not-for-production-use-only-in-ci")
    monkeypatch.setattr(identity_mod, "SessionLocal", made)
    monkeypatch.setattr(tenant_resolution_mod, "SessionLocal", made)
    monkeypatch.setattr("src.ui.enterprise.compose.SessionLocal", made)
    db = made()
    db.add(Organization(id=_ORG, name="Founder Organization", slug="founder-organization", status=OrganizationStatus.ACTIVE))
    db.flush()
    db.add(Organization(id=_OTHER, name="Other Organization", slug="other-organization", status=OrganizationStatus.ACTIVE))
    db.commit()
    db.close()
    return made


def _provision(factory: sessionmaker, email: str = "t01.internal-beta@fixture.invalid") -> None:
    result = provision_member(
        email=email,
        full_name="Internal Beta Tester One",
        password=_PASSWORD,
        organization_slug="founder-organization",
        recovery_secret=_SECRET,
        confirm_member_provision=True,
        session_factory=factory,
    )
    assert result.ok
    assert result.membership_role == MEMBER_ROLE


def test_provision_creates_member_and_is_idempotent(factory: sessionmaker) -> None:
    _provision(factory)
    db = factory()
    user = db.query(User).filter(User.email == "t01.internal-beta@fixture.invalid").one()
    membership = db.query(OrganizationMembership).filter(OrganizationMembership.user_id == user.id).one()
    assert user.role == "member"
    assert membership.role == "member"
    assert membership.status == MembershipStatus.ACTIVE
    assert membership.organization_id == _ORG
    assert verify_password(_PASSWORD, user.hashed_password)
    db.close()
    again = provision_member(
        email="t01.internal-beta@fixture.invalid",
        full_name="Internal Beta Tester One",
        password=_PASSWORD,
        organization_slug="founder-organization",
        recovery_secret=_SECRET,
        confirm_member_provision=True,
        session_factory=factory,
    )
    assert again.outcome == ProvisionOutcome.ALREADY_MEMBER
    db = factory()
    assert db.query(User).count() == 1
    db.close()


def test_provision_does_not_log_password(factory: sessionmaker, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    _provision(factory)
    assert _PASSWORD not in caplog.text


def test_provision_refuses_owner_account(factory: sessionmaker) -> None:
    db = factory()
    owner = User(
        email="owner@fixture.invalid",
        hashed_password="not-used-here-because-login-is-not-called",
        full_name="Existing Owner",
        role="owner",
        is_active=1,
    )
    db.add(owner)
    db.flush()
    db.add(
        OrganizationMembership(
            user_id=owner.id,
            organization_id=_ORG,
            role="owner",
            status=MembershipStatus.ACTIVE,
        )
    )
    db.commit()
    db.close()
    result = provision_member(
        email="owner@fixture.invalid",
        full_name="Existing Owner",
        password=_PASSWORD,
        organization_slug="founder-organization",
        recovery_secret=_SECRET,
        confirm_member_provision=True,
        session_factory=factory,
    )
    assert result.outcome == ProvisionOutcome.PRIVILEGE_ESCALATION_REJECTED
    db = factory()
    kept = db.query(User).filter(User.email == "owner@fixture.invalid").one()
    membership = db.query(OrganizationMembership).filter(OrganizationMembership.user_id == kept.id).one()
    assert kept.role == "owner"
    assert membership.role == "owner"
    db.close()


def test_provision_rejects_unknown_organization(factory: sessionmaker) -> None:
    result = provision_member(
        email="t01.internal-beta@fixture.invalid",
        full_name="Internal Beta Tester One",
        password=_PASSWORD,
        organization_slug="missing-organization",
        recovery_secret=_SECRET,
        confirm_member_provision=True,
        session_factory=factory,
    )
    assert result.outcome == ProvisionOutcome.ORGANIZATION_NOT_FOUND


def test_member_login_resolves_only_founder_organization(factory: sessionmaker) -> None:
    _provision(factory)
    from runner_api import app

    client = TestClient(app)
    response = client.post(
        "/api/v1/identity/login",
        json={"email": "t01.internal-beta@fixture.invalid", "password": _PASSWORD},
    )
    assert response.status_code == 200
    body = response.json()["identity"]
    assert body["principal_kind"] == "HUMAN"
    assert body["is_human"] is True
    assert body["role"] == "member"
    tenant = client.get("/api/v1/tenant/me")
    assert tenant.status_code == 200
    resolved = tenant.json()["tenant"]
    assert resolved["organization_name"] == "Founder Organization"
    assert resolved["membership_role"] == "member"
    assert resolved["membership_status"] == "active"
    denied = client.post("/api/v1/tenant/select", json={"organization_id": str(_OTHER)})
    assert denied.status_code == 403


def test_fixture_pseudonymizes_and_hides_other_tenants(factory: sessionmaker) -> None:
    rows = []
    rows.append(
        {
            "Record ID": "900",
            "Lifecycle Stage": "Opportunity",
            "Associated Company": "KPI Partners",
            "Email": "real.person@example.com",
            "Phone Number": "+1-555-0100",
            "Last Activity Date": "",
            "Type of first engagement": "",
        }
    )
    for index in range(1, 8):
        rows.append(
            {
                "Record ID": f"{index:04d}",
                "Lifecycle Stage": "Lead",
                "Associated Company": f"Company {index}",
                "Email": f"person{index}@example.com",
                "Phone Number": "555-0100",
                "Last Activity Date": "2025-05-09 01:02" if index <= 2 else "",
                "Type of first engagement": "Email" if index <= 2 else "",
            }
        )
    for index in range(8, 12):
        rows.append(
            {
                "Record ID": f"{index:04d}",
                "Lifecycle Stage": "Lead",
                "Associated Company": "",
                "Email": f"person{index}@example.com",
                "Phone Number": "555-0199",
                "Last Activity Date": "",
                "Type of first engagement": "",
            }
        )
    companies = [
        {"Record ID": "c1", "Company name": "Company 1", "Additional Domains": "company1.example", "Phone Number": "555-0000"}
    ]
    selected = select_hubspot_contacts(rows)
    assert all((row.get("Lifecycle Stage") == "Lead") for row in selected)
    assert len(selected) <= 14
    db = factory()
    clay = [
        {
            "name": "Fixture Prospect",
            "domain": "fixture-prospect.example",
            "source_record_id": "clay-screenshot:fixture-prospect",
        }
    ]
    first = seed_commercial_fixture(
        db,
        organization_id=_ORG,
        hubspot_companies=companies,
        hubspot_contacts=rows,
        clay_prospects=clay,
        confirm_staging_fixture=True,
    )
    assert first.opportunities == 0
    assert first.customers == 0
    assert first.qualified == 0
    assert first.unmapped_lifecycle == 1
    assert first.prospects == 1
    stored = db.query(Contact).all()
    blob = " ".join((contact.email or "") + (contact.phone or "") + (contact.notes or "") for contact in stored)
    assert "real.person@example.com" not in blob
    assert "person1@example.com" not in blob
    assert "555-0100" not in blob
    assert "source_system=hubspot" in stored[0].notes
    assert "source_truth=source_derived" in stored[0].notes
    again = seed_commercial_fixture(
        db,
        organization_id=_ORG,
        hubspot_companies=companies,
        hubspot_contacts=rows,
        clay_prospects=clay,
        confirm_staging_fixture=True,
    )
    assert again.contacts == first.contacts
    assert db.query(Contact).count() == first.contacts
    visible = _tenant_companies(str(_ORG))
    assert visible is not None
    assert any(row["name"] == "Fixture Prospect" for row in visible)
    hidden = db.query(Company).filter(Company.organization_id == _OTHER).count()
    assert hidden == 0
    other_visible = _tenant_companies(str(_OTHER))
    assert other_visible == []
    db.close()
