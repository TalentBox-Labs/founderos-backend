"""SaaS S1 — Identity Foundation focused tests on primary runner_api."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import revenue_os.models  # noqa: F401 — register tables
import revenue_os.services.tenant_resolution as tenant_resolution_mod
import runner_api_routers.identity as identity_mod
import runner_api_routers.manual_demand as mdg_router
import tests.test_of1_operator_flow as of1
from revenue_os.auth import create_access_token, hash_password
from revenue_os.models.base import Base
from revenue_os.models.contact import Contact
from revenue_os.models.deal import Deal
from revenue_os.models.organization import (
    MembershipStatus,
    Organization,
    OrganizationMembership,
    OrganizationStatus,
)
from revenue_os.models.user import User
from revenue_os.services.identity_context import (
    AuthMethod,
    IdentityContext,
    PrincipalKind,
    bind_requested_by,
    service_identity,
)
from runner_api import app

ROOT = Path(__file__).resolve().parents[1]
_EMAIL = "s1-owner@example.com"
_PASSWORD = "correct-horse-battery"
_NAME = "S1 Owner"


@pytest.fixture(autouse=True)
def _reset_identity_ephemeral() -> None:
    identity_mod._revoked_jtis.clear()
    identity_mod._login_failures.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def identity_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> sessionmaker:
    engine = create_engine(f"sqlite:///{tmp_path / 's1_identity.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(identity_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(tenant_resolution_mod, "SessionLocal", session_factory)
    return session_factory


@pytest.fixture
def owner_user(identity_db: sessionmaker) -> User:
    """SaaS S2+: require_tenant_mutation() needs an active OrganizationMembership
    for the logged-in user, not just the user row itself."""
    db = identity_db()
    try:
        user = User(
            email=_EMAIL,
            hashed_password=hash_password(_PASSWORD),
            full_name=_NAME,
            role="owner",
            is_active=1,
        )
        db.add(user)
        db.flush()
        org = Organization(name="S1 Org", slug="s1-org", status=OrganizationStatus.ACTIVE)
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
        db.refresh(user)
        db.expunge(user)
        return user
    finally:
        db.close()


def _login(client: TestClient, *, json_api: bool = False) -> None:
    if json_api:
        r = client.post(
            "/api/v1/identity/login",
            json={"email": _EMAIL, "password": _PASSWORD},
        )
        assert r.status_code == 200, r.text
        return
    r = client.post(
        "/login",
        data={"email": _EMAIL, "password": _PASSWORD, "next": "/cockpit"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/cockpit"


def test_valid_human_login_succeeds(client: TestClient, owner_user: User) -> None:
    _login(client)
    me = client.get("/api/v1/identity/me")
    assert me.status_code == 200
    identity = me.json()["identity"]
    assert identity["is_human"] is True
    assert identity["principal_kind"] == "HUMAN"
    assert identity["auth_method"] == "session"
    assert identity["email"] == _EMAIL
    assert identity["display_name"] == _NAME
    assert identity["role"] == "owner"
    assert identity["user_id"] == str(owner_user.id)


def test_invalid_credentials_fail(client: TestClient, owner_user: User) -> None:
    r = client.post(
        "/api/v1/identity/login",
        json={"email": _EMAIL, "password": "wrong-password"},
    )
    assert r.status_code == 401
    me = client.get("/api/v1/identity/me")
    assert me.json()["identity"]["principal_kind"] == "ANONYMOUS"


def test_protected_route_rejects_anonymous(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "1")
    r = client.get("/cockpit", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]


def test_authenticated_human_opens_cockpit(
    client: TestClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "1")
    _login(client)
    r = client.get("/cockpit", follow_redirects=False)
    assert r.status_code == 200
    assert "Executive Cockpit" in r.text
    assert 'data-testid="s1-logout"' in r.text


def test_authenticated_human_opens_operator(
    client: TestClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "1")
    _login(client)
    r = client.get("/operator", follow_redirects=False)
    assert r.status_code == 200
    assert "Operator Flow" in r.text


def test_authenticated_human_opens_demand_registration(
    client: TestClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "1")
    _login(client)
    r = client.get("/operator/demand/register", follow_redirects=False)
    assert r.status_code == 200
    assert "Manual Demand Registration" in r.text


def test_logout_invalidates_identity(
    client: TestClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "1")
    _login(client, json_api=True)
    assert client.get("/api/v1/identity/me").json()["identity"]["is_human"] is True
    out = client.post("/api/v1/identity/logout")
    assert out.status_code == 200
    me = client.get("/api/v1/identity/me").json()["identity"]
    assert me["principal_kind"] == "ANONYMOUS"
    assert me["is_human"] is False
    r = client.get("/cockpit", follow_redirects=False)
    assert r.status_code == 303


def test_requested_by_spoofing_blocked(
    client: TestClient,
    owner_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", "Krishna Founder")
    db = of1._FakeDB()
    db.contacts = []
    db.deals = []
    monkeypatch.setattr(mdg_router, "SessionLocal", lambda: db)
    _login(client)
    r = client.post(
        "/api/v1/mdg/manual-demand/register",
        json={
            "email": "spoof@example.com",
            "name": "Spoof Lead",
            "source": "manual",
            "requested_by": "Founder Impersonator",
        },
    )
    assert r.status_code == 200
    assert r.json()["operator"] == _NAME
    assert r.json()["operator"] != "Founder Impersonator"


def test_service_api_key_cannot_become_human(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNNER_API_KEY", "s1-service-key")
    monkeypatch.delenv("FOUNDER_OS_OPERATOR_NAME", raising=False)
    headers = {"Authorization": "Bearer s1-service-key"}
    me = client.get("/api/v1/identity/me", headers=headers)
    assert me.status_code == 200
    identity = me.json()["identity"]
    assert identity["principal_kind"] == "SERVICE"
    assert identity["is_human"] is False
    assert identity["auth_method"] == "api_key"
    r = client.post(
        "/api/v1/mdg/manual-demand/register",
        json={"email": "svc@example.com", "source": "manual"},
        headers=headers,
    )
    assert r.status_code == 503


def test_agent_identity_cannot_mutate_human_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, identity_db: sessionmaker
) -> None:
    monkeypatch.delenv("FOUNDER_OS_OPERATOR_NAME", raising=False)
    token = create_access_token(
        {
            "sub": "00000000-0000-0000-0000-000000000001",
            "kind": PrincipalKind.AGENT.value,
            "name": "Krishna Founder",
            "jti": "agent-jti",
        }
    )
    client.cookies.set(identity_mod.IDENTITY_COOKIE, token)
    r = client.post(
        "/api/v1/mdg/manual-demand/register",
        json={"email": "agent@example.com", "source": "manual"},
    )
    # Fail-closed: AGENT cookie is not HUMAN and has no SERVICE key → 401 at
    # auth boundary (or 503 if a route reaches human-authority checks).
    assert r.status_code in {401, 503}
    with pytest.raises(PermissionError):
        bind_requested_by(
            IdentityContext(
                principal_kind=PrincipalKind.AGENT,
                auth_method=AuthMethod.SESSION,
                is_human=False,
                display_name="Krishna Founder",
            )
        )


def test_ai_identity_cannot_mutate_human_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, identity_db: sessionmaker
) -> None:
    monkeypatch.delenv("FOUNDER_OS_OPERATOR_NAME", raising=False)
    token = create_access_token(
        {
            "sub": "00000000-0000-0000-0000-000000000002",
            "kind": PrincipalKind.AI.value,
            "name": "Krishna Founder",
            "jti": "ai-jti",
        }
    )
    client.cookies.set(identity_mod.IDENTITY_COOKIE, token)
    r = client.post(
        "/api/v1/cockpit/actions/qualified-demand/accept",
        json={"demand_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
    )
    assert r.status_code in {401, 503}
    with pytest.raises(PermissionError):
        bind_requested_by(
            IdentityContext(
                principal_kind=PrincipalKind.AI,
                auth_method=AuthMethod.SESSION,
                is_human=False,
                display_name="Krishna Founder",
            )
        )


def test_valid_human_mutation_still_passes(
    client: TestClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = of1._FakeDB()
    db.contacts = []
    db.deals = []
    monkeypatch.setattr(mdg_router, "SessionLocal", lambda: db)
    monkeypatch.delenv("FOUNDER_OS_OPERATOR_NAME", raising=False)
    _login(client)
    r = client.post(
        "/api/v1/mdg/manual-demand/register",
        json={"email": "human@example.com", "name": "Human Lead", "source": "manual"},
    )
    assert r.status_code == 200
    assert r.json()["operator"] == _NAME
    assert r.json()["handoff_registered"] is True


def test_identity_context_reaches_frozen_operations(
    client: TestClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = of1._FakeDB()
    db.contacts = []
    db.deals = []
    monkeypatch.setattr(mdg_router, "SessionLocal", lambda: db)
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", "Krishna Founder")
    _login(client)
    r = client.post(
        "/api/v1/mdg/manual-demand/register",
        json={"email": "wrap@example.com", "source": "manual"},
    )
    assert r.status_code == 200
    assert r.json()["operator"] == _NAME
    src = inspect.getsource(mdg_router.register_manual_demand)
    assert "register_marketing_handoff" in src
    assert "_trusted_cockpit_operator" in src


def test_a4_5_contract_unchanged() -> None:
    text = (ROOT / "docs/sales/a4_5/LEADSCORER_CONTACT_STATUS_CONTRACT_v1.0.md").read_text()
    assert "STATUS: FROZEN" in text
    src = inspect.getsource(identity_mod)
    assert "apply_contact_status_update" not in src


def test_a3_5_contract_unchanged() -> None:
    text = (ROOT / "docs/sales/a3_5/A3_5_RUNNER_DEAL_STAGE_CONTRACT_v1.0.md").read_text()
    assert "FROZEN" in text
    src = inspect.getsource(identity_mod)
    assert "apply_deal_stage_update" not in src


def test_mc04_5_contract_unchanged() -> None:
    text = (
        ROOT / "docs/integration/mc04_5/QUALIFIED_DEMAND_HANDOFF_CONTRACT_v1.0.md"
    ).read_text()
    assert "STATUS: FROZEN" in text
    src = inspect.getsource(identity_mod)
    assert "register_marketing_handoff" not in src


def test_mc06_5_contract_unchanged() -> None:
    text = (
        ROOT / "docs/integration/mc06_5/COMMERCIAL_OUTCOME_AUTHORITY_CONTRACT_v1.0.md"
    ).read_text()
    assert "FROZEN" in text or "frozen" in text.lower()
    src = inspect.getsource(identity_mod)
    assert "register_commercial_outcome_handoff" not in src


def test_of1_5_contract_unchanged() -> None:
    text = (ROOT / "docs/operator/of1_5/OPERATOR_FLOW_BASELINE_v1.0.md").read_text()
    assert "FROZEN" in text
    import runner_api_routers.operator_flow as of_router

    src = inspect.getsource(of_router)
    assert src.count("_trusted_cockpit_operator()") == 8


def test_mdg1_5_contract_unchanged() -> None:
    text = (
        ROOT / "docs/marketing/mdg1_5/MANUAL_DEMAND_REGISTRATION_BASELINE_v1.0.md"
    ).read_text()
    assert "FROZEN" in text
    src = inspect.getsource(mdg_router)
    assert "register_marketing_handoff" in src
    assert "requested_by" not in mdg_router.ManualDemandRegisterBody.model_fields


def test_s1_identity_context_remains_non_tenant_s2_extends_separately() -> None:
    """S1.5 IdentityContext stays tenant-free; SaaS S2 adds TenantContext + Organization."""
    from dataclasses import fields as dc_fields

    from revenue_os.services import identity_context as ctx_mod

    field_names = {item.name for item in dc_fields(IdentityContext)}
    assert "tenant_id" not in field_names
    assert "organization_id" not in field_names
    assert "workspace_id" not in field_names
    user_cols = set(User.__table__.columns.keys())
    assert "tenant_id" not in user_cols
    assert "organization_id" not in user_cols
    contact_cols = set(Contact.__table__.columns.keys())
    assert "organization_id" in contact_cols
    assert (ROOT / "revenue_os" / "models" / "organization.py").exists()
    assert "filter_by_tenant" not in inspect.getsource(identity_mod)
    assert "filter_by_organization" not in inspect.getsource(ctx_mod)


def test_login_page_renders(client: TestClient) -> None:
    r = client.get("/login")
    assert r.status_code == 200
    assert 'data-testid="s1-login-form"' in r.text
    assert "SECRET_KEY" not in r.text
    assert "FOUNDER_OS_BOOTSTRAP_PASSWORD" not in r.text
    assert _PASSWORD not in r.text


def test_invalid_token_rejected(client: TestClient, identity_db: sessionmaker) -> None:
    client.cookies.set(identity_mod.IDENTITY_COOKIE, "not-a-jwt")
    me = client.get("/api/v1/identity/me").json()["identity"]
    assert me["principal_kind"] == "ANONYMOUS"


def test_service_identity_cannot_bind_requested_by() -> None:
    with pytest.raises(PermissionError):
        bind_requested_by(service_identity())


def test_no_public_signup_on_runner(client: TestClient) -> None:
    r = client.post(
        "/api/v1/identity/register",
        json={"email": "x@example.com", "password": "x", "full_name": "X"},
    )
    assert r.status_code in {404, 405}


def test_bootstrap_owner_from_env(
    identity_db: sessionmaker, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.setenv("FOUNDER_OS_BOOTSTRAP_EMAIL", "boot@example.com")
    monkeypatch.setenv("FOUNDER_OS_BOOTSTRAP_PASSWORD", "bootstrap-secret-1")
    monkeypatch.setenv("FOUNDER_OS_BOOTSTRAP_NAME", "Bootstrap Owner")
    identity_mod.bootstrap_owner_if_needed()
    r = client.post(
        "/api/v1/identity/login",
        json={"email": "boot@example.com", "password": "bootstrap-secret-1"},
    )
    assert r.status_code == 200
    assert r.json()["identity"]["role"] == "owner"
    assert r.json()["identity"]["display_name"] == "Bootstrap Owner"


def test_login_brute_force_limited(
    client: TestClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_LOGIN_MAX_FAILURES", "3")
    for _ in range(3):
        bad = client.post(
            "/api/v1/identity/login",
            json={"email": _EMAIL, "password": "nope"},
        )
        assert bad.status_code == 401
    blocked = client.post(
        "/api/v1/identity/login",
        json={"email": _EMAIL, "password": _PASSWORD},
    )
    assert blocked.status_code == 429
