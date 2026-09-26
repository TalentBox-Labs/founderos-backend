"""SaaS S1.5 — Identity Foundation Baseline v1.0 freeze suite."""

from __future__ import annotations

import inspect
from dataclasses import fields as dc_fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import revenue_os.models  # noqa: F401
import revenue_os.services.tenant_resolution as tenant_resolution_mod
import runner_api_routers.identity as identity_mod
import runner_api_routers.manual_demand as mdg_router
import tests.test_a3_runner_deal_stage as a3
import tests.test_a4_runner_contact_status as a4
import tests.test_mc04_qualified_demand as mc04
import tests.test_mc06_5_commercial_outcome_baseline_freeze as mc06_5
import tests.test_mdg1_5_manual_demand_baseline_freeze as mdg15
import tests.test_of1_5_operator_flow_baseline_freeze as of15
import tests.test_of1_operator_flow as of1
import tests.test_saas_s1_identity_foundation as s1
import tests.test_ui2_5_cockpit_baseline_freeze as ui25
from revenue_os.auth import create_access_token, hash_password
from revenue_os.config import settings
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
from revenue_os.services import identity_context as ctx_mod
from revenue_os.services.identity_context import (
    MVP_ROLES,
    AuthMethod,
    IdentityContext,
    PrincipalKind,
    bind_requested_by,
    classify_actor_label,
    normalize_role,
    service_identity,
)
from revenue_os.services.mutation_authority import HumanAuthorityError, require_human_mutation_authority
from runner_api import app

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "saas" / "s1_5"

_EMAIL = "s15-owner@example.com"
_PASSWORD = "correct-horse-battery-s15"
_NAME = "S15 Owner"

A1_5_KNOWN_EXCEPTIONS = (
    "tests/test_prospecting_ui.py::test_prospecting_plan_endpoint_returns_stage_allocation_and_leads",
    "tests/test_prospecting_ui.py::test_prospecting_plan_validation_error_for_invalid_score",
)

REQUIRED_ARTIFACTS = (
    "IDENTITY_FOUNDATION_BASELINE_v1.0.md",
    "CANONICAL_USER_CONTRACT_v1.0.md",
    "IDENTITY_CONTEXT_CONTRACT_v1.0.md",
    "AUTHENTICATION_CONTRACT_v1.0.md",
    "AUTH_COOKIE_TOKEN_SECURITY_ATTESTATION.md",
    "HUMAN_SERVICE_AGENT_AI_SEPARATION_v1.0.md",
    "REQUESTED_BY_SERVER_BINDING_v1.0.md",
    "API_KEY_IDENTITY_BOUNDARY_v1.0.md",
    "ROLE_VOCABULARY_v1.0.md",
    "OWNER_BOOTSTRAP_CONTRACT_v1.0.md",
    "TENANT_CONTEXT_EXTENSION_BOUNDARY_v1.0.md",
    "S1_5_KNOWN_TEST_EXCEPTIONS.md",
    "S1_5_RUNTIME_CHANGE_RECONCILIATION.md",
    "S1_5_BASELINE_MANIFEST.md",
)


@pytest.fixture(autouse=True)
def _reset_identity_ephemeral() -> None:
    identity_mod._revoked_jtis.clear()
    identity_mod._login_failures.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def identity_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> sessionmaker:
    engine = create_engine(f"sqlite:///{tmp_path / 's15_identity.db'}")
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
        org = Organization(name="S15 Org", slug="s15-org", status=OrganizationStatus.ACTIVE)
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


def _login(client: TestClient) -> None:
    r = client.post(
        "/api/v1/identity/login",
        json={"email": _EMAIL, "password": _PASSWORD},
    )
    assert r.status_code == 200, r.text


# ── 1–8 auth / routes ───────────────────────────────────────────────────────


def test_freeze_canonical_user_identity_works(owner_user: User) -> None:
    assert owner_user.email == _EMAIL
    assert owner_user.role == "owner"
    assert owner_user.is_active == 1
    assert User.__tablename__ == "users"
    assert "id" in User.__table__.columns
    assert "hashed_password" in User.__table__.columns


def test_freeze_valid_login_works(client: TestClient, owner_user: User) -> None:
    _login(client)
    me = client.get("/api/v1/identity/me").json()["identity"]
    assert me["is_human"] is True
    assert me["principal_kind"] == "HUMAN"
    assert me["email"] == _EMAIL


def test_freeze_invalid_login_fails(client: TestClient, owner_user: User) -> None:
    r = client.post(
        "/api/v1/identity/login",
        json={"email": _EMAIL, "password": "wrong"},
    )
    assert r.status_code == 401


def test_freeze_protected_founder_route_rejects_anonymous(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "1")
    r = client.get("/cockpit", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]


def test_freeze_cockpit_requires_auth(
    client: TestClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "1")
    assert client.get("/cockpit", follow_redirects=False).status_code == 303
    _login(client)
    assert client.get("/cockpit", follow_redirects=False).status_code == 200


def test_freeze_operator_requires_auth(
    client: TestClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "1")
    assert client.get("/operator", follow_redirects=False).status_code == 303
    _login(client)
    assert client.get("/operator", follow_redirects=False).status_code == 200


def test_freeze_demand_registration_requires_auth(
    client: TestClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "1")
    assert client.get("/operator/demand/register", follow_redirects=False).status_code == 303
    _login(client)
    assert client.get("/operator/demand/register", follow_redirects=False).status_code == 200


def test_freeze_logout_invalidates_identity(
    client: TestClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "1")
    _login(client)
    assert client.post("/api/v1/identity/logout").status_code == 200
    assert client.get("/api/v1/identity/me").json()["identity"]["principal_kind"] == "ANONYMOUS"
    assert client.get("/cockpit", follow_redirects=False).status_code == 303


# ── 9–11 tokens / IdentityContext ───────────────────────────────────────────


def test_freeze_expired_token_rejected(client: TestClient, owner_user: User) -> None:
    token = create_access_token(
        {
            "sub": str(owner_user.id),
            "kind": PrincipalKind.HUMAN.value,
            "jti": "expired-jti",
        },
        expires_delta=timedelta(seconds=-10),
    )
    client.cookies.set(identity_mod.IDENTITY_COOKIE, token)
    me = client.get("/api/v1/identity/me").json()["identity"]
    assert me["principal_kind"] == "ANONYMOUS"


def test_freeze_forged_cookie_rejected(client: TestClient, identity_db: sessionmaker) -> None:
    forged = jwt.encode(
        {
            "sub": "00000000-0000-0000-0000-000000000099",
            "kind": "HUMAN",
            "name": "Krishna Founder",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        "wrong-secret-key-not-settings",
        algorithm="HS256",
    )
    client.cookies.set(identity_mod.IDENTITY_COOKIE, forged)
    me = client.get("/api/v1/identity/me").json()["identity"]
    assert me["principal_kind"] == "ANONYMOUS"
    client.cookies.set(identity_mod.IDENTITY_COOKIE, "not.a.jwt")
    assert client.get("/api/v1/identity/me").json()["identity"]["principal_kind"] == "ANONYMOUS"


def test_freeze_identity_context_constructed_correctly(client: TestClient, owner_user: User) -> None:
    _login(client)
    me = client.get("/api/v1/identity/me").json()["identity"]
    assert me["auth_method"] == "session"
    assert me["user_id"] == str(owner_user.id)
    assert me["display_name"] == _NAME
    assert me["role"] == "owner"
    names = {item.name for item in dc_fields(IdentityContext)}
    assert "tenant_id" not in names


# ── 12–16 classification / API key ──────────────────────────────────────────


def test_freeze_human_classification_correct() -> None:
    assert classify_actor_label("Krishna Founder") is PrincipalKind.HUMAN


def test_freeze_service_classification_correct() -> None:
    svc = service_identity()
    assert svc.principal_kind is PrincipalKind.SERVICE
    assert svc.is_human is False
    assert classify_actor_label("system") is PrincipalKind.SERVICE


def test_freeze_agent_classification_correct() -> None:
    assert classify_actor_label("agent:hermes") is PrincipalKind.AGENT


def test_freeze_ai_classification_correct() -> None:
    assert classify_actor_label("ai:copilot") is PrincipalKind.AI
    assert classify_actor_label("AI") is PrincipalKind.AI


def test_freeze_runner_api_key_cannot_create_human(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNNER_API_KEY", "s15-service-key")
    monkeypatch.delenv("FOUNDER_OS_OPERATOR_NAME", raising=False)
    me = client.get(
        "/api/v1/identity/me",
        headers={"Authorization": "Bearer s15-service-key"},
    ).json()["identity"]
    assert me["principal_kind"] == "SERVICE"
    assert me["is_human"] is False


# ── 17–23 binding / HUMAN_ONLY ──────────────────────────────────────────────


def test_freeze_requested_by_spoofing_blocked(
    client: TestClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
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


def test_freeze_server_binding_preserved() -> None:
    src = inspect.getsource(identity_mod.resolve_trusted_human)
    assert "bind_requested_by" in src
    assert "RUNNER_API_KEY never satisfies" in (
        identity_mod.resolve_trusted_human.__doc__ or ""
    )
    cockpit_src = inspect.getsource(
        __import__("runner_api_routers.cockpit", fromlist=["x"])._trusted_cockpit_operator
    )
    assert "resolve_trusted_human" in cockpit_src


def test_freeze_human_only_valid_mutation_still_works(
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


def test_freeze_agent_human_only_mutation_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, identity_db: sessionmaker
) -> None:
    s1.test_agent_identity_cannot_mutate_human_only(client, monkeypatch, identity_db)


def test_freeze_ai_human_only_mutation_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, identity_db: sessionmaker
) -> None:
    s1.test_ai_identity_cannot_mutate_human_only(client, monkeypatch, identity_db)


def test_freeze_service_human_only_mutation_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNNER_API_KEY", "s15-service-key")
    monkeypatch.delenv("FOUNDER_OS_OPERATOR_NAME", raising=False)
    r = client.post(
        "/api/v1/mdg/manual-demand/register",
        json={"email": "svc@example.com", "source": "manual"},
        headers={"Authorization": "Bearer s15-service-key"},
    )
    assert r.status_code == 503
    with pytest.raises(PermissionError):
        bind_requested_by(service_identity())


def test_freeze_direct_authority_bypass_remains_blocked() -> None:
    with pytest.raises(HumanAuthorityError):
        require_human_mutation_authority("agent:hermes")
    with pytest.raises(HumanAuthorityError):
        require_human_mutation_authority("ai:copilot")
    with pytest.raises(HumanAuthorityError):
        require_human_mutation_authority("automation")
    assert require_human_mutation_authority("Krishna Founder") == "Krishna Founder"


# ── 24–28 roles / tenancy ───────────────────────────────────────────────────


def test_freeze_role_vocabulary_preserved() -> None:
    assert MVP_ROLES == ("owner", "admin", "member", "viewer")
    assert normalize_role("OWNER") == "owner"
    assert normalize_role("unknown") == "member"
    doc = (DOCS / "ROLE_VOCABULARY_v1.0.md").read_text(encoding="utf-8")
    assert "DEFERRED_TO_S2" in doc
    assert "OWNER" in doc and "VIEWER" in doc


def test_freeze_role_enforcement_explicitly_deferred() -> None:
    doc = (DOCS / "ROLE_VOCABULARY_v1.0.md").read_text(encoding="utf-8")
    assert "DEFERRED_TO_S2" in doc
    assert "does **not** freeze broad endpoint authorization" in doc or "DEFERRED" in doc
    baseline = (DOCS / "IDENTITY_FOUNDATION_BASELINE_v1.0.md").read_text(encoding="utf-8")
    assert "broad RBAC" in baseline.lower() or "Role vocabulary" in baseline


def test_freeze_s1_5_tenant_boundary_superseded_by_s2() -> None:
    """S1.5 documented tenant as ABSENT; S2 implements Organization + TenantContext."""
    tenant_doc = (DOCS / "TENANT_CONTEXT_EXTENSION_BOUNDARY_v1.0.md").read_text(encoding="utf-8")
    assert "ABSENT" in tenant_doc or "S2" in tenant_doc
    assert (ROOT / "revenue_os" / "models" / "organization.py").exists()
    models_init = (ROOT / "revenue_os" / "models" / "__init__.py").read_text(encoding="utf-8")
    assert "Organization" in models_init
    s2_doc = ROOT / "docs" / "saas" / "s2" / "S2_TENANT_CONTEXT_CONTRACT.md"
    assert s2_doc.exists()


def test_freeze_s2_tenant_owned_columns_bounded() -> None:
    """S2 adds nullable organization_id only on bounded tenant-owned entities."""
    for model in (User,):
        cols = set(model.__table__.columns.keys())
        assert "tenant_id" not in cols
        assert "organization_id" not in cols
    for model in (Contact, Deal):
        cols = set(model.__table__.columns.keys())
        assert "tenant_id" not in cols
        assert "organization_id" in cols


def test_freeze_no_tenant_scoped_queries_in_identity_layer() -> None:
    for mod in (identity_mod, ctx_mod, mdg_router):
        src = inspect.getsource(mod)
        assert "filter_by_tenant" not in src
        assert ".filter_by(tenant" not in src


# ── 29–35 frozen business baselines ─────────────────────────────────────────


def test_freeze_a3_5_unchanged() -> None:
    a3.test_apply_deal_stage_update_rejects_reopen()
    assert "FROZEN" in (
        ROOT / "docs/sales/a3_5/A3_5_RUNNER_DEAL_STAGE_CONTRACT_v1.0.md"
    ).read_text(encoding="utf-8")


def test_freeze_a4_5_unchanged() -> None:
    a4.test_score_contact_does_not_mutate_status()
    assert "STATUS: FROZEN" in (
        ROOT / "docs/sales/a4_5/LEADSCORER_CONTACT_STATUS_CONTRACT_v1.0.md"
    ).read_text(encoding="utf-8")


def test_freeze_mc04_5_unchanged() -> None:
    mc04.test_register_handoff_does_not_create_contact()
    assert "STATUS: FROZEN" in (
        ROOT / "docs/integration/mc04_5/QUALIFIED_DEMAND_HANDOFF_CONTRACT_v1.0.md"
    ).read_text(encoding="utf-8")


def test_freeze_mc06_5_unchanged() -> None:
    mc06_5.test_freeze_no_shared_sot()
    assert "FROZEN" in (
        ROOT / "docs/integration/mc06_5/COMMERCIAL_OUTCOME_AUTHORITY_CONTRACT_v1.0.md"
    ).read_text(encoding="utf-8")


def test_freeze_ui2_5_unchanged() -> None:
    ui25.test_freeze_cockpit_router_only_two_mutations()


def test_freeze_of1_5_unchanged() -> None:
    of15.test_freeze_operator_action_route_set_unchanged()
    src = inspect.getsource(
        __import__("runner_api_routers.operator_flow", fromlist=["x"])
    )
    assert src.count("_trusted_cockpit_operator()") == 8


def test_freeze_mdg1_5_unchanged() -> None:
    mdg15.test_freeze_registration_post_route()
    assert "register_marketing_handoff" in inspect.getsource(mdg_router)


# ── 36–38 credentials / bootstrap / CSRF cookie ─────────────────────────────


def test_freeze_no_credentials_committed() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "FOUNDER_OS_BOOTSTRAP_PASSWORD=" not in example.splitlines() or all(
        line.strip().startswith("#") or "replace" in line.lower() or "BOOTSTRAP_PASSWORD" in line
        for line in example.splitlines()
        if "BOOTSTRAP_PASSWORD" in line
    )
    for line in example.splitlines():
        if "BOOTSTRAP_PASSWORD" in line:
            assert line.strip().startswith("#")
    login_html = (ROOT / "templates" / "login.html").read_text(encoding="utf-8")
    assert "SECRET_KEY" not in login_html
    assert _PASSWORD not in login_html


def test_freeze_bootstrap_owner_path_works(
    identity_db: sessionmaker, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.setenv("FOUNDER_OS_BOOTSTRAP_EMAIL", "s15-boot@example.com")
    monkeypatch.setenv("FOUNDER_OS_BOOTSTRAP_PASSWORD", "bootstrap-secret-s15")
    monkeypatch.setenv("FOUNDER_OS_BOOTSTRAP_NAME", "Bootstrap Owner")
    identity_mod.bootstrap_owner_if_needed()
    identity_mod.bootstrap_owner_if_needed()  # no overwrite
    db = identity_db()
    try:
        rows = db.query(User).filter(User.email == "s15-boot@example.com").all()
        assert len(rows) == 1
        assert rows[0].role == "owner"
    finally:
        db.close()
    r = client.post(
        "/api/v1/identity/login",
        json={"email": "s15-boot@example.com", "password": "bootstrap-secret-s15"},
    )
    assert r.status_code == 200


def test_freeze_csrf_cookie_security_invariants(
    client: TestClient, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_COOKIE_SECURE", "true")
    r = client.post(
        "/api/v1/identity/login",
        json={"email": _EMAIL, "password": _PASSWORD},
    )
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert identity_mod.IDENTITY_COOKIE in set_cookie
    assert "HttpOnly" in set_cookie or "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    src = inspect.getsource(identity_mod._set_identity_cookie)
    assert 'samesite="lax"' in src
    assert "httponly=True" in src
    assert "Secure" in set_cookie or "secure" in set_cookie.lower()
    attest = (DOCS / "AUTH_COOKIE_TOKEN_SECURITY_ATTESTATION.md").read_text(encoding="utf-8")
    assert "SameSite" in attest
    assert "PARTIAL" in attest
    assert settings.SECRET_KEY


# ── artifacts / runtime / exceptions / public website ───────────────────────


def test_freeze_artifacts_present() -> None:
    for name in REQUIRED_ARTIFACTS:
        assert (DOCS / name).is_file(), name
    baseline = (DOCS / "IDENTITY_FOUNDATION_BASELINE_v1.0.md").read_text(encoding="utf-8")
    assert "STATUS: FROZEN" in baseline
    assert "v1.0" in baseline


def test_freeze_runtime_change_reconciliation_documented() -> None:
    doc = (DOCS / "S1_5_RUNTIME_CHANGE_RECONCILIATION.md").read_text(encoding="utf-8")
    assert "Runtime Changes = **2**" in doc or "Runtime Changes:** **2**" in doc or "2/2" in doc
    assert "bind_identity_request_context" in doc
    assert "FOUNDER_OS_REQUIRE_LOGIN" in doc or "bootstrap" in doc.lower()
    assert "PASS" in doc


def test_freeze_a1_5_exceptions_register_unchanged() -> None:
    exceptions = (DOCS / "S1_5_KNOWN_TEST_EXCEPTIONS.md").read_text(encoding="utf-8")
    for item in A1_5_KNOWN_EXCEPTIONS:
        assert item in exceptions
    assert "OperationalError" in exceptions or "connection refused" in exceptions.lower()


def test_freeze_public_website_not_login_gated() -> None:
    """Public marketing site is Cloudflare Pages — not runner_api.

    Founder + Content Ops HTML surfaces on runner_api are login-gated via
    founder_login_redirect. Cloudflare Pages remains a separate public surface.
    """
    src = inspect.getsource(identity_mod.founder_login_redirect)
    ui_mod = __import__("runner_api_routers.ui", fromlist=["x"])
    ui_src = inspect.getsource(ui_mod)
    assert "founder_login_redirect" in ui_src
    assert "founder_login_redirect" in inspect.getsource(ui_mod.page_cockpit)
    assert "founder_login_redirect" in inspect.getsource(ui_mod.page_dashboard)
    assert "founder_login_redirect" in inspect.getsource(ui_mod.page_content_studio)
    auth_doc = (DOCS / "AUTHENTICATION_CONTRACT_v1.0.md").read_text(encoding="utf-8")
    assert "Cloudflare Pages" in auth_doc or "separate" in auth_doc.lower()


def test_freeze_cookie_name_and_algorithm_stable() -> None:
    assert identity_mod.IDENTITY_COOKIE == "founder_os_identity"
    token_src = inspect.getsource(identity_mod._issue_human_token)
    assert "create_access_token" in token_src
    assert 'algorithm="HS256"' in inspect.getsource(
        __import__("revenue_os.auth", fromlist=["x"]).create_access_token
    ) or "HS256" in inspect.getsource(
        __import__("revenue_os.auth", fromlist=["x"])
    )
