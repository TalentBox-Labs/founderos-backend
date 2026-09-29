"""P0 Content Ops service-authority and single-organization beta lock.

Proves a service principal, a client organization id, or a human-looking
approver/requested_by string cannot become HUMAN or tenant authority.
"""

from __future__ import annotations

import inspect
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from revenue_os.auth import hash_password
from revenue_os.models.organization import (
    MembershipStatus,
    Organization,
    OrganizationMembership,
    OrganizationStatus,
)
from revenue_os.models.user import User
from revenue_os.services import content_ops_authority as authority
from revenue_os.services.content_ops_authority import (
    CONTENT_OPS_BETA_ORGANIZATION_ENV,
    TENANT_MODEL,
)
from revenue_os.services.tenant_resolution import ORGANIZATION_COOKIE
import revenue_os.services.tenant_resolution as tenant_resolution_mod
import runner_api_routers.identity as identity_mod
from runner_api import app
from runner_api_routers.utils import require_human_or_api_key

_PASSWORD = "ContentOpsP0Authority1!"
_NAME = "Krishna Founder"
_SERVICE_KEY = "content-ops-p0-service-key"

_HUMAN_MUTATIONS = (
    ("POST", "/api/v1/editorial/W99/approve", {"approver": _NAME, "notes": "x", "phase": "2b"}),
    ("POST", "/api/v1/editorial/W99/reject", {"approver": _NAME, "notes": "x"}),
    ("POST", "/api/v1/editorial/W99/request-changes", {"approver": _NAME, "notes": "x", "staging_root": "output/generated/W99"}),
    ("POST", "/api/v1/publishing/jobs", {"content_id": "W99", "channel": "website", "requested_by": _NAME}),
    ("POST", "/api/v1/publishing/pub_missing/publish", {"requested_by": _NAME}),
    ("POST", "/api/v1/publishing/pub_missing/cancel", {"requested_by": _NAME}),
    ("POST", "/api/v1/publishing/pub_missing/retry", {"requested_by": _NAME}),
    ("POST", "/marketing/publish", {"confirmed": True, "status_path": "output/marketing/x/08_Publish_Status.json"}),
)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def identity_db(monkeypatch: pytest.MonkeyPatch) -> sessionmaker:
    from revenue_os.database import engine

    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(identity_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(tenant_resolution_mod, "SessionLocal", session_factory)
    return session_factory


def _user(
    db,
    *,
    name: str = _NAME,
) -> tuple[User, str]:
    email = f"p0-{uuid.uuid4().hex}@talentbox.invalid"
    user = User(
        email=email,
        hashed_password=hash_password(_PASSWORD),
        full_name=name,
        role="owner",
        is_active=1,
    )
    db.add(user)
    db.flush()
    return user, email


def _org(db, label: str) -> Organization:
    org = Organization(
        name=label,
        slug=f"{label}-{uuid.uuid4().hex[:8]}",
        status=OrganizationStatus.ACTIVE,
    )
    db.add(org)
    db.flush()
    return org


def _member(db, user: User, org: Organization, role: str = "owner") -> None:
    db.add(
        OrganizationMembership(
            user_id=user.id,
            organization_id=org.id,
            role=role,
            status=MembershipStatus.ACTIVE,
        )
    )


def _login(client: TestClient, email: str) -> None:
    response = client.post(
        "/api/v1/identity/login",
        json={"email": email, "password": _PASSWORD},
    )
    assert response.status_code == 200, response.text


def _service_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_SERVICE_KEY}"}


def test_tenant_model_is_explicit_single_org_beta_lock() -> None:
    assert TENANT_MODEL == "EXPLICIT_SINGLE_ORG_BETA_LOCK"
    source = inspect.getsource(authority.resolve_content_ops_tenant)
    assert "query_params" not in source
    assert "cookies" not in source
    assert "headers" not in source
    assert "X-Organization" not in source


def test_beta_binding_ignores_request_and_fails_closed_when_ambiguous() -> None:
    only = uuid.uuid4()

    class _Query:
        def __init__(self, rows: list[tuple[uuid.UUID]]) -> None:
            self._rows = rows

        def filter(self, *_args, **_kwargs):
            return self

        def all(self) -> list[tuple[uuid.UUID]]:
            return self._rows

    class _DB:
        def __init__(self, rows: list[tuple[uuid.UUID]], org: object | None) -> None:
            self._rows = rows
            self._org = org

        def query(self, *_args):
            return _Query(self._rows)

        def get(self, _model, _org_id):
            return self._org

    class _Org:
        status = OrganizationStatus.ACTIVE

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.delenv(CONTENT_OPS_BETA_ORGANIZATION_ENV, raising=False)
    try:
        assert authority._beta_organization_id(_DB([(only,)], _Org())) == only
        assert (
            authority._beta_organization_id(_DB([(only,), (uuid.uuid4(),)], _Org()))
            is None
        )
    finally:
        monkeypatch.undo()


def test_anonymous_content_read_and_mutation_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RUNNER_API_KEY", raising=False)
    read = client.get("/api/v1/content-studio/content")
    assert read.status_code == 401
    write = client.post(
        "/api/v1/editorial/W99/approve",
        json={"approver": _NAME, "notes": "anon"},
    )
    assert write.status_code == 401


def test_anonymous_oauth_state_is_not_tenant_authority(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RUNNER_API_KEY", raising=False)
    response = client.get(
        "/api/v1/editorial/pending",
        params={"state": "oauth-state", "code": "oauth-code", "organization_id": str(uuid.uuid4())},
    )
    assert response.status_code == 401


def test_valid_human_member_beta_operation(
    client: TestClient,
    identity_db: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNNER_API_KEY", raising=False)
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "1")
    db = identity_db()
    try:
        user, email = _user(db)
        org = _org(db, "beta")
        _member(db, user, org)
        db.commit()
        org_id = str(org.id)
    finally:
        db.close()
    monkeypatch.setenv(CONTENT_OPS_BETA_ORGANIZATION_ENV, org_id)
    _login(client, email)

    page = client.get("/content-studio", follow_redirects=False)
    assert page.status_code == 200
    listing = client.get("/api/v1/content-studio/content")
    assert listing.status_code == 200

    seen: dict[str, str] = {}

    def _capture(content_id: str, decision: str, body) -> dict[str, bool]:
        seen["content_id"] = content_id
        seen["decision"] = decision
        seen["approver"] = body.approver
        return {"ok": True}

    monkeypatch.setattr("runner_api_routers.editorial._run_decision", _capture)
    approved = client.post(
        "/api/v1/editorial/W99/approve",
        json={
            "approver": "Impersonated Operator",
            "notes": "typed name is not authority",
            "phase": "2b",
            "organization_id": str(uuid.uuid4()),
        },
    )
    assert approved.status_code == 200, approved.text
    assert seen["approver"] == _NAME
    assert seen["decision"] == "approve"


def test_human_non_member_denied(
    client: TestClient,
    identity_db: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNNER_API_KEY", raising=False)
    db = identity_db()
    try:
        beta = _org(db, "beta")
        other = _org(db, "other")
        _member(db, _user(db)[0], other)
        outsider, outsider_email = _user(db)
        _member(db, outsider, other)
        db.commit()
        beta_id = str(beta.id)
    finally:
        db.close()
    monkeypatch.setenv(CONTENT_OPS_BETA_ORGANIZATION_ENV, beta_id)
    _login(client, outsider_email)
    response = client.get("/api/v1/content-studio/content")
    assert response.status_code == 403


def test_service_principal_denied_human_content_operations(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNNER_API_KEY", _SERVICE_KEY)
    called = {"publish": 0, "decision": 0}

    def _publish(*_args, **_kwargs):
        called["publish"] += 1
        raise AssertionError("service reached publication mutation")

    def _decision(*_args, **_kwargs):
        called["decision"] += 1
        raise AssertionError("service reached editorial mutation")

    monkeypatch.setattr("src.tools.publishing_engine.create_publish_job", _publish)
    monkeypatch.setattr("src.tools.publishing_engine.manual_publish", _publish)
    monkeypatch.setattr("src.tools.publishing_engine.cancel_job", _publish)
    monkeypatch.setattr("src.tools.publishing_engine.retry_job", _publish)
    monkeypatch.setattr("runner_api_routers.editorial._run_decision", _decision)
    monkeypatch.setattr(
        "runner_api_routers.marketing._social_publisher",
        lambda: (_ for _ in ()).throw(AssertionError("service reached marketing publish")),
    )

    read = client.get("/api/v1/content-studio/content", headers=_service_headers())
    assert read.status_code == 403
    for method, path, payload in _HUMAN_MUTATIONS:
        response = client.request(method, path, headers=_service_headers(), json=payload)
        assert response.status_code == 403, (path, response.status_code, response.text)
    assert called == {"publish": 0, "decision": 0}

    identity = client.get("/api/v1/identity/me", headers=_service_headers()).json()["identity"]
    assert identity["principal_kind"] == "SERVICE"
    assert identity["is_human"] is False
    tenant = client.get("/api/v1/tenant/me", headers=_service_headers())
    assert tenant.json().get("tenant") is None


def test_service_human_looking_approver_cannot_publish_or_approve(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNNER_API_KEY", _SERVICE_KEY)

    def _boom(*_args, **_kwargs):
        raise AssertionError("human-looking label upgraded SERVICE")

    monkeypatch.setattr("src.tools.publishing_engine.create_publish_job", _boom)
    monkeypatch.setattr("src.tools.publishing_engine.manual_publish", _boom)
    monkeypatch.setattr("runner_api_routers.editorial._run_decision", _boom)
    headers = _service_headers()
    approve = client.post(
        "/api/v1/editorial/W99/approve",
        headers=headers,
        json={"approver": "Krishna Founder", "notes": "looks human", "phase": "2b"},
    )
    publish = client.post(
        "/api/v1/publishing/jobs",
        headers=headers,
        json={
            "content_id": "W99",
            "channel": "website",
            "requested_by": "Krishna Founder",
            "organization_id": str(uuid.uuid4()),
        },
    )
    assert approve.status_code == 403
    assert publish.status_code == 403


@pytest.mark.parametrize(
    "spoof",
    ["body", "query", "header"],
)
def test_client_org_spoof_does_not_become_tenant(
    client: TestClient,
    identity_db: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
    spoof: str,
) -> None:
    monkeypatch.delenv("RUNNER_API_KEY", raising=False)
    db = identity_db()
    try:
        beta = _org(db, "beta")
        foreign = _org(db, "foreign")
        member, member_email = _user(db)
        outsider, outsider_email = _user(db)
        _member(db, member, beta)
        _member(db, outsider, foreign)
        db.commit()
        beta_id = str(beta.id)
        foreign_id = str(foreign.id)
    finally:
        db.close()
    monkeypatch.setenv(CONTENT_OPS_BETA_ORGANIZATION_ENV, beta_id)
    monkeypatch.setattr(
        "runner_api_routers.editorial._run_decision",
        lambda *_args, **_kwargs: {"ok": True},
    )

    def _send(email: str, claimed_org: str):
        _login(client, email)
        if spoof == "body":
            return client.post(
                "/api/v1/editorial/W99/reject",
                json={"approver": _NAME, "notes": "spoof", "organization_id": claimed_org},
            )
        if spoof == "query":
            return client.get(
                "/api/v1/content-studio/content",
                params={"organization_id": claimed_org, "tenant_id": claimed_org},
            )
        return client.get(
            "/api/v1/content-studio/content",
            headers={"X-Organization-Id": claimed_org, "X-Tenant-Id": claimed_org},
        )

    outsider_response = _send(outsider_email, beta_id)
    assert outsider_response.status_code == 403

    client.cookies.clear()
    member_response = _send(member_email, foreign_id)
    assert member_response.status_code == 200, member_response.text


def test_zero_membership_fail_closed(
    client: TestClient,
    identity_db: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNNER_API_KEY", raising=False)
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "1")
    db = identity_db()
    try:
        beta = _org(db, "beta")
        _user_only, email = _user(db)
        db.commit()
        beta_id = str(beta.id)
    finally:
        db.close()
    monkeypatch.setenv(CONTENT_OPS_BETA_ORGANIZATION_ENV, beta_id)
    _login(client, email)
    api = client.get("/api/v1/editorial/pending")
    page = client.get("/editorial", follow_redirects=False)
    assert api.status_code == 403
    assert page.status_code == 403


def test_multi_org_ambiguity_fail_closed(
    client: TestClient,
    identity_db: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNNER_API_KEY", raising=False)
    db = identity_db()
    try:
        beta = _org(db, "beta")
        second = _org(db, "second")
        user, email = _user(db)
        _member(db, user, beta)
        _member(db, user, second)
        db.commit()
        beta_id = str(beta.id)
    finally:
        db.close()
    monkeypatch.setenv(CONTENT_OPS_BETA_ORGANIZATION_ENV, beta_id)
    _login(client, email)
    client.cookies.set(ORGANIZATION_COOKIE, beta_id)
    response = client.get(
        "/api/v1/publishing/jobs",
        headers={"X-Organization-Id": beta_id},
        params={"organization_id": beta_id},
    )
    assert response.status_code == 403


def test_unpinned_multi_org_deployment_fail_closed(
    client: TestClient,
    identity_db: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNNER_API_KEY", raising=False)
    monkeypatch.delenv(CONTENT_OPS_BETA_ORGANIZATION_ENV, raising=False)
    db = identity_db()
    try:
        first = _org(db, "one")
        _org(db, "two")
        user, email = _user(db)
        _member(db, user, first)
        db.commit()
    finally:
        db.close()
    _login(client, email)
    response = client.get("/api/v1/content-studio/content")
    assert response.status_code == 403


def test_cross_tenant_read_and_write_denied(
    client: TestClient,
    identity_db: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNNER_API_KEY", raising=False)
    db = identity_db()
    try:
        org_a = _org(db, "org-a")
        org_b = _org(db, "org-b")
        _member(db, _user(db)[0], org_a)
        intruder, intruder_email = _user(db)
        _member(db, intruder, org_b)
        db.commit()
        org_a_id = str(org_a.id)
    finally:
        db.close()
    monkeypatch.setenv(CONTENT_OPS_BETA_ORGANIZATION_ENV, org_a_id)

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("cross-tenant write reached the editorial engine")

    monkeypatch.setattr("runner_api_routers.editorial._run_decision", _forbidden)
    monkeypatch.setattr("src.tools.publishing_engine.create_publish_job", _forbidden)
    _login(client, intruder_email)
    read = client.get(
        "/api/v1/content-studio/content",
        headers={"X-Organization-Id": org_a_id},
        params={"organization_id": org_a_id},
    )
    write = client.post(
        "/api/v1/editorial/W99/approve",
        json={"approver": _NAME, "notes": "cross", "organization_id": org_a_id},
    )
    publish = client.post(
        "/api/v1/publishing/jobs",
        json={
            "content_id": "W99",
            "channel": "website",
            "requested_by": _NAME,
            "organization_id": org_a_id,
        },
    )
    assert read.status_code == 403
    assert write.status_code == 403
    assert publish.status_code == 403


def test_browser_cookie_cannot_nominate_tenant(
    client: TestClient,
    identity_db: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNNER_API_KEY", raising=False)
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "1")
    db = identity_db()
    try:
        beta = _org(db, "beta")
        other = _org(db, "other")
        member, member_email = _user(db)
        outsider, outsider_email = _user(db)
        _member(db, member, beta)
        _member(db, outsider, other)
        db.commit()
        beta_id = str(beta.id)
        other_id = str(other.id)
    finally:
        db.close()
    monkeypatch.setenv(CONTENT_OPS_BETA_ORGANIZATION_ENV, beta_id)

    _login(client, outsider_email)
    client.cookies.set(ORGANIZATION_COOKIE, beta_id)
    denied = client.get("/content-studio", follow_redirects=False)
    assert denied.status_code == 403

    client.cookies.clear()
    _login(client, member_email)
    client.cookies.set(ORGANIZATION_COOKIE, other_id)
    allowed = client.get("/publishing", follow_redirects=False)
    assert allowed.status_code == 200


def test_content_ops_templates_do_not_nominate_organization() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "templates"
    names = (
        "editorial_detail.html",
        "editorial_pending.html",
        "publishing_detail.html",
        "publishing_queue.html",
        "content_studio.html",
        "content_studio_detail.html",
    )
    for name in names:
        text = (root / name).read_text(encoding="utf-8").lower()
        assert "organization_id" not in text
        assert "x-organization-id" not in text


def test_human_mutation_routes_use_content_ops_gate() -> None:
    guarded_prefixes = (
        "/api/v1/editorial",
        "/api/v1/publishing",
        "/api/v1/content-studio",
        "/api/v1/seo",
        "/api/v1/analytics",
        "/api/v1/mcp/hub",
        "/marketing/",
        "/run",
        "/validate",
        "/generate",
        "/edit",
        "/switch-week",
        "/go-live",
        "/run-pipeline",
    )
    mutation_methods = {"POST", "PUT", "PATCH", "DELETE"}
    seen = 0

    def _routes():
        for route in app.routes:
            if type(route).__name__ == "_IncludedRouter":
                yield from route.original_router.routes
            else:
                yield route

    for route in _routes():
        path = getattr(route, "path", "")
        methods = set(getattr(route, "methods", set()) or set())
        if not methods.intersection(mutation_methods):
            continue
        def _under(prefix: str) -> bool:
            if path == prefix:
                return True
            if prefix.endswith("/"):
                return path.startswith(prefix)
            return path.startswith(prefix + "/")

        if not any(_under(prefix) for prefix in guarded_prefixes):
            continue
        dependant = getattr(route, "dependant", None)
        assert dependant is not None, path
        calls = [dep.call for dep in dependant.dependencies]
        assert require_human_or_api_key in calls, path
        if any(token in path for token in ("approve", "reject", "cancel", "pause", "publish")):
            seen += 1
    assert seen >= 4
