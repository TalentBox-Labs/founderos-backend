"""MDG1 — Manual Founder Demand Registration focused tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import revenue_os.models  # noqa: F401 — register tables
import revenue_os.services.tenant_resolution as tenant_resolution_mod
import runner_api_routers.identity as identity_mod
import runner_api_routers.manual_demand as mdg_router
import tests.test_of1_operator_flow as of1
from revenue_os.auth import hash_password
from revenue_os.models.base import Base
from revenue_os.models.contact import ContactStatus
from revenue_os.models.organization import (
    MembershipStatus,
    Organization,
    OrganizationMembership,
    OrganizationStatus,
)
from revenue_os.models.user import User
from revenue_os.services.qualified_demand_service import (
    ACTION_HANDOFF as QD_HANDOFF,
    accept_qualified_demand,
)
from runner_api import app

_OPERATOR = "Krishna Founder"
_OPERATOR_EMAIL = "mdg1-operator@example.com"
_OPERATOR_PASSWORD = "correct-horse-battery"
_EMAIL = "mdg1@example.com"
_DEMAND_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def operator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)


@pytest.fixture
def human_session(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """SaaS S2+: mutations resolve tenant from a real logged-in session with an
    active OrganizationMembership — FOUNDER_OS_OPERATOR_NAME alone is legacy
    fallback only and no longer satisfies require_tenant_mutation()."""
    engine = create_engine(f"sqlite:///{tmp_path / 'mdg1_identity.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(identity_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(tenant_resolution_mod, "SessionLocal", session_factory)

    db = session_factory()
    try:
        user = User(
            email=_OPERATOR_EMAIL,
            hashed_password=hash_password(_OPERATOR_PASSWORD),
            full_name=_OPERATOR,
            role="owner",
            is_active=1,
        )
        db.add(user)
        db.flush()
        org = Organization(name="MDG1 Org", slug="mdg1-org", status=OrganizationStatus.ACTIVE)
        db.add(org)
        db.flush()
        db.add(
            OrganizationMembership(
                user_id=user.id,
                organization_id=org.id,
                role="owner",
                status=MembershipStatus.ACTIVE,
            )
        )
        db.commit()
    finally:
        db.close()

    r = client.post(
        "/login",
        data={"email": _OPERATOR_EMAIL, "password": _OPERATOR_PASSWORD, "next": "/cockpit"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text


def _db() -> of1._FakeDB:
    db = of1._FakeDB()
    db.contacts = []
    db.deals = []
    return db


def test_registration_ui_opens(client: TestClient, operator_env: None) -> None:
    r = client.get("/operator/demand/register")
    assert r.status_code == 200
    assert "Manual Demand Registration" in r.text
    assert "Founder OS" in r.text
    assert 'data-testid="mdg1-form"' in r.text
    assert "requested_by" not in r.text.lower() or "not taken from this form" in r.text


def test_valid_manual_demand_registers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None, human_session: None
) -> None:
    db = _db()
    monkeypatch.setattr(mdg_router, "SessionLocal", lambda: db)
    r = client.post(
        "/api/v1/mdg/manual-demand/register",
        json={"email": _EMAIL, "name": "MDG Lead", "source": "manual"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["handoff_registered"] is True
    assert body["operator"] == _OPERATOR
    assert body["contact_created"] is False
    assert body["deal_created"] is False
    assert body["revenue_mutated"] is False
    assert body["public_capture"] is False
    assert any(log.action_type == QD_HANDOFF for log in db.logs)


def test_reuses_mc04_5_register_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None, human_session: None
) -> None:
    db = _db()
    monkeypatch.setattr(mdg_router, "SessionLocal", lambda: db)
    r = client.post(
        "/api/v1/mdg/manual-demand/register",
        json={"email": _EMAIL, "demand_id": _DEMAND_ID},
    )
    assert r.status_code == 200
    assert r.json()["demand_id"] == _DEMAND_ID
    handoff = next(log for log in db.logs if log.action_type == QD_HANDOFF)
    assert handoff.target_id == _DEMAND_ID
    assert handoff.detail["payload"]["source"] == "manual"
    assert handoff.detail["payload"]["channel"] == "manual_founder_registration"


def test_canonical_contact_only_after_accept(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None, human_session: None
) -> None:
    db = _db()
    monkeypatch.setattr(mdg_router, "SessionLocal", lambda: db)
    r = client.post(
        "/api/v1/mdg/manual-demand/register",
        json={"email": _EMAIL, "name": "After Accept", "demand_id": _DEMAND_ID},
    )
    assert r.status_code == 200
    assert db.contacts == []
    result = accept_qualified_demand(db, _DEMAND_ID, _OPERATOR)
    assert result["created"] is True
    assert db.contacts[0].email == _EMAIL
    assert db.contacts[0].status == ContactStatus.LEAD
    assert db.deals == []


def test_spoofed_requested_by_ignored(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None, human_session: None
) -> None:
    db = _db()
    monkeypatch.setattr(mdg_router, "SessionLocal", lambda: db)
    r = client.post(
        "/api/v1/mdg/manual-demand/register",
        json={
            "email": _EMAIL,
            "requested_by": "agent:spoof",
            "human": True,
            "approved": True,
        },
    )
    assert r.status_code == 200
    assert r.json()["requested_by"] == _OPERATOR
    assert r.json()["operator"] == _OPERATOR
    assert "requested_by" not in mdg_router.ManualDemandRegisterBody.model_fields


def test_agent_mutation_blocked(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", "agent")
    r = client.post(
        "/api/v1/mdg/manual-demand/register",
        json={"email": _EMAIL},
    )
    assert r.status_code == 503


def test_ai_mutation_blocked(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", "ai:copilot")
    r = client.post(
        "/api/v1/mdg/manual-demand/register",
        json={"email": _EMAIL},
    )
    assert r.status_code == 503


def test_direct_service_bypass_still_blocked() -> None:
    of1.test_ui1_1_service_bypass_still_blocked()


def test_validation_requires_email(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None
) -> None:
    monkeypatch.setattr(mdg_router, "SessionLocal", lambda: _db())
    r = client.post("/api/v1/mdg/manual-demand/register", json={"name": "No Email"})
    assert r.status_code == 422


def test_validation_rejects_bad_source(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None
) -> None:
    monkeypatch.setattr(mdg_router, "SessionLocal", lambda: _db())
    r = client.post(
        "/api/v1/mdg/manual-demand/register",
        json={"email": _EMAIL, "source": "invented_channel"},
    )
    assert r.status_code == 422


def test_idempotent_retry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None, human_session: None
) -> None:
    db = _db()
    monkeypatch.setattr(mdg_router, "SessionLocal", lambda: db)
    first = client.post(
        "/api/v1/mdg/manual-demand/register",
        json={"email": _EMAIL, "demand_id": _DEMAND_ID},
    )
    second = client.post(
        "/api/v1/mdg/manual-demand/register",
        json={"email": _EMAIL, "demand_id": _DEMAND_ID},
    )
    assert first.status_code == 200
    assert first.json()["idempotent"] is False
    assert second.status_code == 200
    assert second.json()["idempotent"] is True
    assert len([log for log in db.logs if log.action_type == QD_HANDOFF]) == 1


def test_provenance_truthful_no_fabricated_utm(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None, human_session: None
) -> None:
    db = _db()
    monkeypatch.setattr(mdg_router, "SessionLocal", lambda: db)
    r = client.post(
        "/api/v1/mdg/manual-demand/register",
        json={
            "email": _EMAIL,
            "source": "referral",
            "source_detail": "Alice intro",
            "context_note": "Interested in hiring systems",
            "demand_id": _DEMAND_ID,
        },
    )
    assert r.status_code == 200
    payload = db.logs[0].detail["payload"]
    attr = payload["content_attribution"]
    assert attr["registration_mode"] == "manual_founder_ui"
    assert attr["manually_supplied"] is True
    assert attr["source_detail"] == "Alice intro"
    assert "utm_source" not in attr
    assert "utm_campaign" not in attr
    assert payload.get("consent") is None
    assert "utm" not in (payload.get("channel") or "").lower()


def test_no_automatic_deal_or_revenue(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None, human_session: None
) -> None:
    db = _db()
    monkeypatch.setattr(mdg_router, "SessionLocal", lambda: db)
    r = client.post(
        "/api/v1/mdg/manual-demand/register",
        json={"email": _EMAIL, "demand_id": _DEMAND_ID},
    )
    assert r.status_code == 200
    assert db.deals == []
    assert r.json()["deal_created"] is False
    assert r.json()["revenue_mutated"] is False
    accept_qualified_demand(db, _DEMAND_ID, _OPERATOR)
    assert db.deals == []


def test_no_public_anonymous_path(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDER_OS_OPERATOR_NAME", raising=False)
    r = client.post(
        "/api/v1/mdg/manual-demand/register",
        json={"email": _EMAIL},
    )
    assert r.status_code == 503
    # No separate public form route
    page = client.get("/operator/demand/register")
    assert page.status_code == 200
    assert "public form" in page.text.lower() or "No public form" in page.text
    assert "/api/v1/mdg/manual-demand/register" in page.text


@pytest.mark.real_api_auth
def test_unauthenticated_when_api_key_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None
) -> None:
    monkeypatch.setenv("RUNNER_API_KEY", "mdg1-secret")
    r = client.post(
        "/api/v1/mdg/manual-demand/register",
        json={"email": _EMAIL},
    )
    assert r.status_code == 401


def test_of1_5_operator_router_unchanged() -> None:
    """MDG1 must not expand frozen OF1 operator_flow POST set."""
    import inspect

    import runner_api_routers.operator_flow as of_router
    from tests.test_of1_5_operator_flow_baseline_freeze import (
        FROZEN_OPERATOR_POST_ROUTES,
        _full_route_path,
    )

    posts = {
        _full_route_path(route, of_router.router.prefix)
        for route in of_router.router.routes
        if hasattr(route, "methods") and "POST" in route.methods
    }
    assert posts == FROZEN_OPERATOR_POST_ROUTES
    assert "register_marketing_handoff" not in inspect.getsource(of_router)


def test_operator_page_links_to_registration(client: TestClient) -> None:
    r = client.get("/operator")
    assert r.status_code == 200
    assert 'data-testid="mdg1-register-link"' in r.text
    assert "no Audience→Demand capture" in r.text


def test_mc04_5_accept_still_required(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None
) -> None:
    """Registration alone is not Sales accept — preserve authority boundary."""
    db = _db()
    monkeypatch.setattr(mdg_router, "SessionLocal", lambda: db)
    client.post(
        "/api/v1/mdg/manual-demand/register",
        json={"email": _EMAIL, "demand_id": _DEMAND_ID},
    )
    assert db.contacts == []
    assert not any(log.action_type == "qualified_demand_accepted" for log in db.logs)


def test_jinja_shell_not_react(client: TestClient, operator_env: None) -> None:
    r = client.get("/operator/demand/register")
    assert r.status_code == 200
    assert "Founder OS" in r.text
    assert "createRoot" not in r.text
    assert 'data-testid="mdg1-form"' in r.text
