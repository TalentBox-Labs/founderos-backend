"""UI-D1 — Founder OS live demo vertical slice tests."""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import revenue_os.models  # noqa: F401
import runner_api_routers.identity as identity_mod
import runner_api_routers.ui as ui_mod
from revenue_os.auth import hash_password
from revenue_os.models.base import Base
from revenue_os.models.contact import Contact, ContactSource, ContactStatus
from revenue_os.models.organization import (
    MembershipStatus,
    Organization,
    OrganizationMembership,
    OrganizationStatus,
)
from revenue_os.models.user import User
from revenue_os.services.tenant_resolution import ORGANIZATION_COOKIE
from runner_api import app

_ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_CONTACT_A = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_CONTACT_B = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
_OPERATOR = "Krishna Founder"
_DEMAND_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


@pytest.fixture(autouse=True)
def _reset_identity() -> None:
    identity_mod._revoked_jtis.clear()
    identity_mod._login_failures.clear()


@pytest.fixture
def client(cms_client: TestClient) -> TestClient:
    return cms_client


@pytest.fixture
def tenant_db(monkeypatch: pytest.MonkeyPatch, tmp_path) -> sessionmaker:
    engine = create_engine(f"sqlite:///{tmp_path / 'ui_d1.db'}")
    Base.metadata.create_all(bind=engine)
    sf = sessionmaker(bind=engine)
    import revenue_os.database as db_mod
    import revenue_os.services.activity_log as al
    import revenue_os.services.approvals as approvals_mod
    import revenue_os.services.cockpit_read_model as cockpit_rm
    import revenue_os.services.founder_ui_read_model as founder_rm
    import revenue_os.services.operator_flow_read_model as of_rm
    import revenue_os.services.revenue_orchestration_service as rev_svc
    import revenue_os.services.tenant_resolution as tr

    for mod in (
        db_mod,
        tr,
        approvals_mod,
        al,
        ui_mod,
        cockpit_rm,
        founder_rm,
        of_rm,
        rev_svc,
        identity_mod,
    ):
        if hasattr(mod, "SessionLocal"):
            monkeypatch.setattr(mod, "SessionLocal", sf)
    return sf


def _seed_tenants(db_factory: sessionmaker) -> dict[str, str]:
    db = db_factory()
    try:
        org_a = Organization(id=_ORG_A, name="Workspace A", slug="org-a", status=OrganizationStatus.ACTIVE)
        org_b = Organization(id=_ORG_B, name="Workspace B", slug="org-b", status=OrganizationStatus.ACTIVE)
        user_a = User(
            email="owner-a@example.com",
            hashed_password=hash_password("pass-a"),
            full_name=_OPERATOR,
            role="owner",
            is_active=1,
        )
        user_b = User(
            email="owner-b@example.com",
            hashed_password=hash_password("pass-b"),
            full_name="Owner B",
            role="owner",
            is_active=1,
        )
        db.add_all([org_a, org_b, user_a, user_b])
        db.flush()
        db.add_all(
            [
                OrganizationMembership(
                    user_id=user_a.id,
                    organization_id=org_a.id,
                    role="owner",
                    status=MembershipStatus.ACTIVE,
                ),
                OrganizationMembership(
                    user_id=user_b.id,
                    organization_id=org_b.id,
                    role="owner",
                    status=MembershipStatus.ACTIVE,
                ),
            ]
        )
        db.add_all(
            [
                Contact(
                    id=_CONTACT_A,
                    organization_id=_ORG_A,
                    first_name="Contact",
                    last_name="A",
                    email="a@example.com",
                    status=ContactStatus.LEAD,
                    lead_score=80,
                    source=ContactSource.WEB_FORM,
                ),
                Contact(
                    id=_CONTACT_B,
                    organization_id=_ORG_B,
                    first_name="Contact",
                    last_name="B",
                    email="b@example.com",
                    status=ContactStatus.LEAD,
                    lead_score=60,
                    source=ContactSource.WEB_FORM,
                ),
            ]
        )
        db.commit()
        return {"org_a": str(_ORG_A), "org_b": str(_ORG_B), "user_a": str(user_a.id)}
    finally:
        db.close()


def _login(client: TestClient, email: str, password: str, org_id: str) -> None:
    r = client.post(
        "/api/v1/identity/login",
        json={"email": email, "password": password},
    )
    assert r.status_code == 200
    client.cookies.set(ORGANIZATION_COOKIE, org_id)


def test_cockpit_renders_with_attention_items(client: TestClient) -> None:
    """Regression: Jinja must iterate attention queue items (data['items'])."""
    fake_snap = {
        "ok": True,
        "generated_at": "2026-08-18T00:00:00Z",
        "panels": {
            "attention": {
                "state": "ok",
                "message": "",
                "data": {
                    "items": [
                        {
                            "kind": "qualified_demand",
                            "priority": "high",
                            "label": "Demand",
                            "detail": "test@example.com",
                            "meta": {"demand_id": _DEMAND_ID},
                        }
                    ],
                    "count": 1,
                },
            },
            "sales": {
                "state": "ok",
                "message": "",
                "data": {
                    "contact_total": 0,
                    "deal_total": 0,
                    "contacts_by_status": {},
                    "high_score_review": [],
                },
            },
            "marketing_seo": {
                "state": "ok",
                "message": "",
                "data": {
                    "readiness": {"ready_count": 0, "blocked_count": 0},
                    "technical": {"score": 0},
                    "qualified_demand_pending": 1,
                    "social": {"state": "blocked", "message": "x"},
                },
            },
            "commercial_flow": {"state": "ok", "message": "", "data": {"stages": []}},
            "governance": {
                "state": "ok",
                "message": "",
                "data": {"heartbeat": {"enabled": False, "jobs_registered": 0}},
            },
        },
        "errors": [],
    }
    with patch("runner_api_routers.ui.founder_login_redirect", return_value=None):
        with patch("runner_api_routers.ui.build_cockpit_snapshot", return_value=fake_snap):
            r = client.get("/cockpit")
    assert r.status_code == 200
    assert 'data-testid="attention-item"' in r.text


def test_founder_routes_load(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runner_api_routers.ui.founder_login_redirect", lambda _r: None)
    for path in ("/command", "/demand", "/pending-approvals", "/activity"):
        r = client.get(path)
        assert r.status_code == 200, path


def test_command_center_page(
    client: TestClient, tenant_db: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _seed_tenants(tenant_db)
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "0")
    _login(client, "owner-a@example.com", "pass-a", ids["org_a"])
    r = client.get("/command")
    assert "Command Center" in r.text
    assert 'data-testid="command-approvals"' in r.text


def test_contact_workspace_not_found(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runner_api_routers.ui.founder_login_redirect", lambda _r: None)
    with patch("runner_api_routers.ui.resolve_tenant_context", return_value=None):
        r = client.get(f"/contacts/{_CONTACT_A}")
    assert r.status_code == 200
    assert 'data-testid="contact-unavailable"' in r.text or 'data-testid="contact-not-found"' in r.text


def test_tenant_b_cannot_view_tenant_a_contact(
    client: TestClient, tenant_db: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _seed_tenants(tenant_db)
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "0")
    _login(client, "owner-b@example.com", "pass-b", ids["org_b"])
    r = client.get(f"/contacts/{_CONTACT_A}")
    assert r.status_code == 200
    assert 'data-testid="contact-not-found"' in r.text


def test_tenant_a_can_view_own_contact(
    client: TestClient, tenant_db: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _seed_tenants(tenant_db)
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "0")
    _login(client, "owner-a@example.com", "pass-a", ids["org_a"])
    r = client.get(f"/contacts/{_CONTACT_A}")
    assert r.status_code == 200
    assert 'data-testid="contact-workflow"' in r.text
    assert "a@example.com" in r.text


def test_nav_includes_founder_shell(client: TestClient) -> None:
    r = client.get("/login")
    assert r.status_code == 200
    assert 'href="/home"' in r.text
    assert ">Home<" in r.text
    assert ">Command Center<" not in r.text


def test_meeting_interest_display_only(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Booking eligibility visible; no booking UI controls."""
    monkeypatch.setattr("runner_api_routers.ui.founder_login_redirect", lambda _r: None)
    workspace = {
        "generated_at": "now",
        "state": "ok",
        "message": "",
        "contact": {
            "id": str(_CONTACT_A),
            "name": "Test",
            "email": "t@example.com",
            "status": "lead",
            "lead_score": 70,
            "company": None,
            "phone": None,
        },
        "deals": [],
        "follow_up": {"state": "empty", "message": "none"},
        "reply": {
            "state": "ok",
            "meeting_interest": True,
            "booking_eligible": True,
            "booking_status": "eligible",
            "reply_type": "MEETING_INTEREST",
            "recommended_next_action": "BOOKING_ELIGIBLE",
        },
        "timeline": [],
        "workflow": {
            "research": "done",
            "draft": "done",
            "approval": "active",
            "send": "pending",
            "follow_up": "pending",
            "reply": "done",
        },
        "pending_approvals": [],
    }
    with patch(
        "runner_api_routers.ui.build_contact_workspace_snapshot",
        return_value=workspace,
    ):
        r = client.get(f"/contacts/{_CONTACT_A}")
    assert r.status_code == 200
    assert "Booking eligible" in r.text
    # No workspace.booking data seeded -> UI-D2's real booking panel falls
    # back to its "not eligible" governance state; no actionable UI shown.
    assert 'data-testid="booking-not-eligible"' in r.text
    assert "Not eligible for booking yet" in r.text
