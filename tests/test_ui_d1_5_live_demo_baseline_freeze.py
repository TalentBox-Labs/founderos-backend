"""UI-D1.5 — Live Demo Surface Baseline v1.0 freeze suite.

Certification only. Does not redesign UI-D1. Frozen routes, cockpit fix,
tenant isolation, human authority, booking negative scope, demo seed bounds.
"""

from __future__ import annotations

import inspect
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import revenue_os.models  # noqa: F401
import runner_api_routers.identity as identity_mod
import runner_api_routers.ui as ui_mod
from revenue_os.auth import hash_password
from revenue_os.models.approvals import ApprovalRequest
from revenue_os.models.automation_state import AgentActionLog
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

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "ui" / "d1_5"
TEMPLATES = ROOT / "templates"

_ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_CONTACT_A = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_CONTACT_B = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
_OPERATOR = "Krishna Founder"
_APPROVAL_A = "11111111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_SECRET_MARKER = "super-secret-connector-token-xyz"

FROZEN_PRIMARY_ROUTES = (
    "/login",
    "/command",
    "/demand",
    "/contacts/{id}",
    "/pending-approvals",
    "/activity",
    "/operator",
)

FROZEN_DOCS = (
    "FOUNDER_OS_UI_D1_5_LIVE_DEMO_BASELINE_v1.0.md",
    "FOUNDER_OS_UI_D1_5_SCREEN_CONTRACT_v1.0.md",
    "FOUNDER_OS_UI_D1_5_NAVIGATION_CONTRACT_v1.0.md",
    "FOUNDER_OS_UI_D1_5_TENANT_SECURITY_ATTESTATION_v1.0.md",
    "FOUNDER_OS_UI_D1_5_HUMAN_AUTHORITY_ATTESTATION_v1.0.md",
    "FOUNDER_OS_UI_D1_5_COCKPIT_FIX_ATTESTATION_v1.0.md",
    "FOUNDER_OS_UI_D1_5_DEMO_DATA_ATTESTATION_v1.0.md",
    "FOUNDER_OS_UI_D1_5_BOOKING_NEGATIVE_SCOPE_v1.0.md",
    "FOUNDER_OS_UI_D1_5_BROWSER_DEMO_CONTRACT_v1.0.md",
    "FOUNDER_OS_UI_D1_5_REGRESSION_RECONCILIATION_v1.0.md",
    "FOUNDER_OS_UI_D1_5_BASELINE_MANIFEST.md",
)


@pytest.fixture(autouse=True)
def _reset_identity() -> None:
    identity_mod._revoked_jtis.clear()
    identity_mod._login_failures.clear()


@pytest.fixture
def client(cms_client: TestClient) -> TestClient:
    return cms_client


@pytest.fixture
def tenant_db(monkeypatch: pytest.MonkeyPatch, tmp_path) -> sessionmaker:
    engine = create_engine(f"sqlite:///{tmp_path / 'ui_d1_5.db'}")
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
        org_a = Organization(
            id=_ORG_A, name="Workspace A", slug="org-a", status=OrganizationStatus.ACTIVE
        )
        org_b = Organization(
            id=_ORG_B, name="Workspace B", slug="org-b", status=OrganizationStatus.ACTIVE
        )
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
        db.add(
            AgentActionLog(
                actor="orchestrator",
                action_type="rev_orch_reply_assessment",
                target_type="contact",
                target_id=str(_CONTACT_A),
                status="completed",
                organization_id=_ORG_A,
                detail={
                    "assessment": {
                        "reply_type": "MEETING_INTEREST",
                        "summary": "Tenant A private reply summary",
                        "meeting_interest": True,
                    },
                    "routing": {
                        "reply_type": "MEETING_INTEREST",
                        "meeting_interest": True,
                        "booking_eligible": True,
                        "recommended_next_action": "BOOKING_ELIGIBLE",
                    },
                },
            )
        )
        db.add(
            AgentActionLog(
                actor="owner-a",
                action_type="qualified_demand_accepted",
                target_type="contact",
                target_id=str(_CONTACT_A),
                status="completed",
                organization_id=_ORG_A,
                detail={"secret": _SECRET_MARKER},
            )
        )
        db.add(
            ApprovalRequest(
                id=_APPROVAL_A,
                requested_by="workflow",
                action_type="send_outreach_email",
                title="Tenant A outreach draft",
                description="Private to org A",
                target_type="contact",
                target_id=str(_CONTACT_A),
                payload={"organization_id": str(_ORG_A), "contact_id": str(_CONTACT_A)},
                status="pending",
            )
        )
        db.commit()
        return {"org_a": str(_ORG_A), "org_b": str(_ORG_B)}
    finally:
        db.close()


def _login(client: TestClient, email: str, password: str, org_id: str) -> None:
    r = client.post("/api/v1/identity/login", json={"email": email, "password": password})
    assert r.status_code == 200
    client.cookies.set(ORGANIZATION_COOKIE, org_id)


def test_freeze_docs_present() -> None:
    for name in FROZEN_DOCS:
        assert (DOCS / name).is_file(), name


def test_freeze_seven_primary_screens() -> None:
    src = inspect.getsource(ui_mod)
    assert '@router.get("/command"' in src
    assert '@router.get("/demand"' in src
    assert '@router.get("/contacts/{contact_id}"' in src
    assert '@router.get("/pending-approvals"' in src
    assert '@router.get("/activity"' in src
    assert '@router.get("/operator"' in src
    assert '@router.get("/cockpit"' in src
    assert len(FROZEN_PRIMARY_ROUTES) == 7


def test_login_route_renders(client: TestClient) -> None:
    r = client.get("/login")
    assert r.status_code == 200
    assert "Founder OS sign in" in r.text
    assert 'data-testid="s1-login-form"' in r.text


def test_command_center_empty(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runner_api_routers.ui.founder_login_redirect", lambda _r: None)
    empty = {
        "generated_at": "now",
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
        "decision_loop": {
            "total": 0,
            "requires_founder": 0,
            "ready": 0,
            "completed": 0,
            "informational": 0,
        },
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
    with patch("runner_api_routers.ui.build_command_center_snapshot", return_value=empty):
        r = client.get("/command")
    assert r.status_code == 200
    assert "Command Center" in r.text
    assert "No approvals waiting" in r.text
    assert "No pending demand" in r.text
    assert "No replies assessed yet" in r.text
    assert "No meeting interest detected" in r.text


def test_command_center_populated(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runner_api_routers.ui.founder_login_redirect", lambda _r: None)
    snap = {
        "generated_at": "now",
        "state": "ok",
        "message": "",
        "pending_approvals": [{"title": "Draft to Alex", "action_type": "send_outreach_email", "target_id": "c1"}],
        "pending_approval_count": 1,
        "pending_demands": [{"name": "New Demand", "email": "n@x.com", "source": "manual", "demand_id": "d1"}],
        "pending_demand_count": 1,
        "pipeline": {"contacts": 2, "deals": 1, "deals_by_stage": {"discovery": 1}},
        "recent_replies": [{"contact_id": str(_CONTACT_A), "reply_type": "INTERESTED", "summary": "yes"}],
        "meeting_interest": [{"contact_id": str(_CONTACT_A), "booking_status": "eligible", "reply_type": "MEETING_INTEREST"}],
        "meeting_booking_pending": [],
        "follow_up_signals": [{"name": "Alex", "state": "ELIGIBLE", "eligible": True, "contact_id": "c1"}],
        "recent_activity": [{"label": "Demand registered", "created_at": "now"}],
        "decision_items": [],
        "decision_loop": {
            "total": 0,
            "requires_founder": 0,
            "ready": 0,
            "completed": 0,
            "informational": 0,
        },
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
    with patch("runner_api_routers.ui.build_command_center_snapshot", return_value=snap):
        r = client.get("/command")
    assert r.status_code == 200
    assert "Draft to Alex" in r.text
    assert "New Demand" in r.text
    assert "Ready to propose a meeting" in r.text
    assert "randint" not in r.text


def test_cockpit_attention_items_regression(client: TestClient) -> None:
    src = (TEMPLATES / "cockpit.html").read_text()
    assert "data['items']" in src
    assert "data.items" not in src.replace("data['items']", "")
    fake_snap = {
        "ok": True,
        "generated_at": "now",
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
                            "detail": "x@y.com",
                            "meta": {"demand_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
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


def test_demand_screen_renders(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runner_api_routers.ui.founder_login_redirect", lambda _r: None)
    r = client.get("/demand")
    assert r.status_code == 200
    assert 'data-testid="demand-pending"' in r.text
    assert 'data-testid="contacts-list"' in r.text


def test_contact_workspace_valid_tenant(
    client: TestClient, tenant_db: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _seed_tenants(tenant_db)
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "0")
    _login(client, "owner-a@example.com", "pass-a", ids["org_a"])
    r = client.get(f"/contacts/{_CONTACT_A}")
    assert r.status_code == 200
    assert 'data-testid="contact-workflow"' in r.text
    assert "a@example.com" in r.text
    assert "advisory only" in r.text.lower() or "Recommended" in r.text


def test_cross_tenant_contact_blocked(
    client: TestClient, tenant_db: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _seed_tenants(tenant_db)
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "0")
    _login(client, "owner-b@example.com", "pass-b", ids["org_b"])
    r = client.get(f"/contacts/{_CONTACT_A}")
    assert r.status_code == 200
    assert 'data-testid="contact-not-found"' in r.text
    assert "a@example.com" not in r.text
    assert "Tenant A private reply summary" not in r.text


def test_latest_assessment_cross_tenant_blocked(
    client: TestClient, tenant_db: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _seed_tenants(tenant_db)
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "0")
    _login(client, "owner-b@example.com", "pass-b", ids["org_b"])
    r = client.get(f"/contacts/{_CONTACT_A}")
    assert "Tenant A private reply summary" not in r.text
    assert "MEETING_INTEREST" not in r.text
    own = client.get(f"/contacts/{_CONTACT_B}")
    assert own.status_code == 200
    assert 'data-testid="contact-workflow"' in own.text
    assert "No inbound reply assessed yet" in own.text


def test_approvals_tenant_safe(
    client: TestClient, tenant_db: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _seed_tenants(tenant_db)
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "0")
    _login(client, "owner-b@example.com", "pass-b", ids["org_b"])
    r = client.get("/pending-approvals")
    assert r.status_code == 200
    assert "Tenant A outreach draft" not in r.text
    _login(client, "owner-a@example.com", "pass-a", ids["org_a"])
    a = client.get("/pending-approvals")
    assert "Tenant A outreach draft" in a.text


def test_activity_tenant_safe(
    client: TestClient, tenant_db: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _seed_tenants(tenant_db)
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "0")
    _login(client, "owner-b@example.com", "pass-b", ids["org_b"])
    r = client.get("/activity")
    assert r.status_code == 200
    assert str(_CONTACT_A) not in r.text
    assert _SECRET_MARKER not in r.text
    _login(client, "owner-a@example.com", "pass-a", ids["org_a"])
    a = client.get("/activity")
    assert "Contact accepted" in a.text
    assert _SECRET_MARKER not in a.text


def test_no_optional_data_crashes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runner_api_routers.ui.founder_login_redirect", lambda _r: None)
    empty_ws = {
        "generated_at": "now",
        "state": "ok",
        "message": "",
        "contact": {
            "id": str(_CONTACT_A),
            "name": "Empty",
            "email": "e@x.com",
            "status": "lead",
            "lead_score": 0,
            "company": None,
            "phone": None,
        },
        "deals": [],
        "follow_up": {"state": "empty", "message": "No follow-up eligibility data"},
        "reply": {
            "state": "empty",
            "message": "No reply assessment yet",
            "meeting_interest": False,
            "booking_eligible": False,
        },
        "timeline": [],
        "workflow": {
            "research": "pending",
            "draft": "pending",
            "approval": "pending",
            "send": "pending",
            "follow_up": "pending",
            "reply": "pending",
        },
        "pending_approvals": [],
    }
    with patch("runner_api_routers.ui.build_contact_workspace_snapshot", return_value=empty_ws):
        r = client.get(f"/contacts/{_CONTACT_A}")
    assert r.status_code == 200
    assert "No deals linked" in r.text
    assert "No inbound reply assessed yet" in r.text
    assert "No follow-up eligibility data" in r.text
    for path in ("/command", "/demand", "/pending-approvals", "/activity", "/operator"):
        page = client.get(path)
        assert page.status_code == 200, path


def test_booking_eligibility_shown_without_booking_action(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            "summary": "wants a call",
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
    with patch("runner_api_routers.ui.build_contact_workspace_snapshot", return_value=workspace):
        r = client.get(f"/contacts/{_CONTACT_A}")
    assert r.status_code == 200
    assert "Meeting interest" in r.text
    assert "Booking eligible" in r.text
    # No workspace.booking data seeded -> UI-D2's real booking panel falls
    # back to its "not eligible" governance state; no actionable UI shown.
    assert 'data-testid="booking-not-eligible"' in r.text
    assert "Not eligible for booking yet" in r.text
    lower = r.text.lower()
    assert "book a meeting" not in lower
    assert "select a slot" not in lower


def test_no_fake_availability_ui() -> None:
    # founder_approvals.html legitimately uses book_meeting as the real,
    # governed ApprovalRequest.action_type value (UI-D2's specialized
    # rendering) — not fake availability copy. Every other surface should
    # still have none of it.
    for name in (
        "founder_contact.html",
        "founder_command.html",
        "founder_activity.html",
        "founder_demand.html",
    ):
        text = (TEMPLATES / name).read_text().lower()
        assert "book_meeting" not in text
        assert "slot selection" not in text
        assert "calendar booking" not in text

    approvals_text = (TEMPLATES / "founder_approvals.html").read_text().lower()
    assert "slot selection" not in approvals_text
    assert "calendar booking" not in approvals_text


def test_demo_seed_bounded_to_development() -> None:
    seed = (ROOT / "scripts" / "seed_founder_demo.py").read_text()
    startup = (ROOT / "runner_api.py").read_text()
    assert "seed_founder_demo" not in startup
    assert "Does not run on application startup" in seed
    assert 'if __name__ == "__main__"' in seed
    assert "init_db" in seed
    assert "register_marketing_handoff" in seed


def test_human_authority_preserved() -> None:
    approvals_js = (TEMPLATES / "founder_approvals.html").read_text()
    contact_js = (TEMPLATES / "founder_contact.html").read_text()
    # requested_by_label is a server-computed display label, not a client-
    # supplied requested_by field — excluded before the bare-field check.
    assert "requested_by" not in approvals_js.replace("requested_by_label", "")
    assert "decided_by" not in approvals_js
    assert 'JSON.stringify({})' in approvals_js
    assert "requested_by" not in contact_js
    assert "advisory only" in contact_js.lower()
    ui_src = inspect.getsource(ui_mod.page_founder_approvals)
    assert "build_approvals_snapshot" in ui_src


def test_client_tenant_spoof_blocked(
    client: TestClient, tenant_db: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _seed_tenants(tenant_db)
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "0")
    _login(client, "owner-b@example.com", "pass-b", ids["org_b"])
    client.cookies.set(ORGANIZATION_COOKIE, ids["org_a"])
    r = client.get(f"/contacts/{_CONTACT_A}")
    assert r.status_code in (200, 403)
    assert "a@example.com" not in r.text
    if r.status_code == 200:
        assert (
            'data-testid="contact-not-found"' in r.text
            or 'data-testid="contact-unavailable"' in r.text
            or "Not a member" in r.text
            or "Organization context required" in r.text
        )


def test_no_secret_exposure(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("runner_api_routers.ui.founder_login_redirect", lambda _r: None)
    forbidden = (
        "hashed_password",
        "SECRET_KEY",
        "RUNNER_API_KEY",
        "Authorization: Bearer",
        "connector secret",
        _SECRET_MARKER,
    )
    for path in ("/login", "/command", "/demand", "/pending-approvals", "/activity", "/operator"):
        r = client.get(path)
        assert r.status_code == 200, path
        for token in forbidden:
            assert token not in r.text, f"{path} leaked {token}"


def test_cross_tenant_approval_mutate_blocked(
    client: TestClient, tenant_db: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _seed_tenants(tenant_db)
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "0")
    _login(client, "owner-b@example.com", "pass-b", ids["org_b"])
    r = client.post(
        f"/api/v1/approvals/{_APPROVAL_A}/approve",
        json={"decided_by": "spoof-owner"},
    )
    assert r.status_code in (403, 404, 409)
    db = tenant_db()
    try:
        row = db.get(ApprovalRequest, _APPROVAL_A)
        assert row is not None
        assert row.status == "pending"
    finally:
        db.close()


def test_organization_visibility_when_tenant_present(
    client: TestClient, tenant_db: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _seed_tenants(tenant_db)
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "0")
    _login(client, "owner-a@example.com", "pass-a", ids["org_a"])
    r = client.get("/command")
    assert r.status_code == 200
    assert 'data-testid="founder-org-name"' in r.text
    assert "Workspace A" in r.text


def test_nav_primary_product_language(client: TestClient) -> None:
    r = client.get("/login")
    assert 'href="/home"' in r.text
    assert 'href="/os/revenue/contacts"' in r.text
    assert 'href="/os/marketing/campaigns"' in r.text
    assert 'href="/pending-approvals"' in r.text
    assert 'href="/os/operations/activity"' in r.text
    assert 'href="/os/system"' in r.text
    assert ">Command Center<" not in r.text
    assert ">Executive Cockpit<" not in r.text
    assert "TenantContext" not in r.text
    assert "AgentActionLog" not in r.text
    assert "MC04.5" not in r.text
