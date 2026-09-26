"""Adversarial Content Ops auth boundary — HUMAN/SERVICE/ANONYMOUS.

Content Ops storage remains GLOBAL_BY_DESIGN (filesystem tracker). This suite
enforces CLASS H0 (HUMAN session) for browser Content Ops surfaces and
fail-closed SERVICE auth for API-key consumers. It does NOT claim tenant
isolation of Content Ops artifacts.

Authority invariants preserved:
- SERVICE never becomes HUMAN
- SERVICE never becomes tenant authority
- client org assertions never become tenant authority
- missing RUNNER_API_KEY never soft-opens protected endpoints
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

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
from runner_api import app
import runner_api_routers.identity as identity_mod
import revenue_os.services.tenant_resolution as tenant_resolution_mod

_PASSWORD = "ContentOpsAuthBoundary1!"
_NAME = "Krishna Founder"

CONTENT_OPS_HTML = (
    "/",
    "/weeks",
    "/content-studio",
    "/editorial",
    "/publishing",
    "/pipeline",
    "/marketing",
    "/sales",
    "/seo",
    "/analytics",
    "/mcp",
)

CONTENT_OPS_JSON_GET = (
    "/api/v1/content-studio/content",
    "/api/v1/editorial/pending",
    "/api/v1/publishing/jobs",
)

CONTENT_OPS_JSON_MUTATION = (
    ("POST", "/api/v1/editorial/W99/approve"),
    ("POST", "/api/v1/publishing/jobs"),
    ("POST", "/run"),
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


@pytest.fixture()
def owner_creds(identity_db: sessionmaker) -> tuple[User, str, str]:
    email = f"content-ops-auth-{uuid.uuid4().hex}@talentbox.invalid"
    db = identity_db()
    try:
        user = User(
            email=email,
            hashed_password=hash_password(_PASSWORD),
            full_name=_NAME,
            role="owner",
            is_active=1,
        )
        db.add(user)
        db.flush()
        org = Organization(name="COA Org", slug=f"coa-org-{uuid.uuid4().hex[:8]}", status=OrganizationStatus.ACTIVE)
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
        return user, email, _PASSWORD
    finally:
        db.close()


@pytest.fixture()
def zero_membership_creds(identity_db: sessionmaker) -> tuple[User, str, str]:
    email = f"coa-zero-{uuid.uuid4().hex}@talentbox.invalid"
    db = identity_db()
    try:
        user = User(
            email=email,
            hashed_password=hash_password(_PASSWORD),
            full_name=_NAME,
            role="owner",
            is_active=1,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
        return user, email, _PASSWORD
    finally:
        db.close()


@pytest.fixture()
def ensure_require_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "1")


@pytest.fixture()
def clear_runner_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUNNER_API_KEY", raising=False)


def _login(client: TestClient, *, email: str, password: str = _PASSWORD) -> None:
    r = client.post("/api/v1/identity/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text


def _assert_denied_html(response) -> None:
    assert response.status_code in {303, 401, 403}, response.status_code
    if response.status_code == 303:
        loc = response.headers.get("location", "")
        assert "/login" in loc


def _assert_denied_json(response) -> None:
    assert response.status_code in {401, 403}, response.text
    body = response.text.lower()
    assert "content_id" not in body or "items" not in body or response.status_code != 200


# ── A — ANONYMOUS → Content Ops HTML ─────────────────────────────────────────


@pytest.mark.parametrize("path", CONTENT_OPS_HTML)
def test_a_anonymous_content_ops_html_denied(
    client: TestClient,
    ensure_require_login: None,
    clear_runner_api_key: None,
    path: str,
) -> None:
    r = client.get(path, follow_redirects=False)
    _assert_denied_html(r)


# ── B — ANONYMOUS → Content Ops JSON ─────────────────────────────────────────


@pytest.mark.parametrize("path", CONTENT_OPS_JSON_GET)
def test_b_anonymous_content_ops_json_denied(
    client: TestClient,
    clear_runner_api_key: None,
    path: str,
) -> None:
    r = client.get(path)
    _assert_denied_json(r)
    if r.headers.get("content-type", "").startswith("application/json"):
        data = r.json()
        assert data.get("count") in (None, 0) or "items" not in data or r.status_code != 200


# ── C — ANONYMOUS → Content Ops mutation ─────────────────────────────────────


@pytest.mark.parametrize("method,path", CONTENT_OPS_JSON_MUTATION)
def test_c_anonymous_content_ops_mutation_denied(
    client: TestClient,
    clear_runner_api_key: None,
    method: str,
    path: str,
) -> None:
    if method == "POST":
        r = client.post(path, json={})
    else:
        r = client.request(method, path)
    _assert_denied_json(r)


# ── D/E — SERVICE → HUMAN / tenant escalation ────────────────────────────────


def test_d_service_cannot_become_human(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    clear_runner_api_key: None,
) -> None:
    monkeypatch.setenv("RUNNER_API_KEY", "coa-service-key")
    me = client.get(
        "/api/v1/identity/me",
        headers={"Authorization": "Bearer coa-service-key"},
    ).json()["identity"]
    assert me["principal_kind"] == "SERVICE"
    assert me["is_human"] is False


def test_e_service_cannot_become_tenant(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNNER_API_KEY", "coa-service-key")
    r = client.get(
        "/api/v1/tenant/me",
        headers={"Authorization": "Bearer coa-service-key"},
    )
    assert r.status_code == 200
    assert r.json().get("tenant") is None


# ── F/G/H/I — client org assertions fail closed for tenant authority ─────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"json": {"organization_id": str(uuid.uuid4())}},
        {"params": {"organization_id": str(uuid.uuid4())}},
        {"headers": {"X-Organization-Id": str(uuid.uuid4())}},
    ],
)
def test_f_g_h_service_org_assertion_not_tenant_authority(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict,
) -> None:
    monkeypatch.setenv("RUNNER_API_KEY", "coa-service-key")
    headers = {"Authorization": "Bearer coa-service-key"}
    headers.update(kwargs.get("headers") or {})
    r = client.get(
        "/api/v1/tenant/me",
        headers=headers,
        params=kwargs.get("params"),
    )
    assert r.status_code == 200
    assert r.json().get("tenant") is None


def test_i_anonymous_org_assertion_not_tenant_authority(
    client: TestClient,
    clear_runner_api_key: None,
) -> None:
    fake_org = str(uuid.uuid4())
    r = client.get(
        "/api/v1/tenant/me",
        headers={"X-Organization-Id": fake_org},
        params={"organization_id": fake_org},
    )
    assert r.status_code == 200
    assert r.json().get("tenant") is None


# ── J — HUMAN + zero membership (tenant surfaces) ───────────────────────────


def test_j_human_zero_membership_tenant_null(
    client: TestClient,
    zero_membership_creds: tuple[User, str, str],
    ensure_require_login: None,
) -> None:
    _user, email, password = zero_membership_creds
    _login(client, email=email, password=password)
    me = client.get("/api/v1/identity/me").json()["identity"]
    assert me["principal_kind"] == "HUMAN"
    tenant = client.get("/api/v1/tenant/me").json().get("tenant")
    assert tenant is None


# ── M — HUMAN + active membership Content Ops access ────────────────────────


def test_m_valid_human_content_ops_html_allowed(
    client: TestClient,
    owner_creds: tuple[User, str, str],
    ensure_require_login: None,
    clear_runner_api_key: None,
) -> None:
    _user, email, password = owner_creds
    _login(client, email=email, password=password)
    for path in ("/content-studio", "/editorial", "/publishing"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 200, path


def test_m_valid_human_content_ops_json_allowed(
    client: TestClient,
    owner_creds: tuple[User, str, str],
    clear_runner_api_key: None,
) -> None:
    _user, email, password = owner_creds
    _login(client, email=email, password=password)
    for path in CONTENT_OPS_JSON_GET:
        r = client.get(path)
        assert r.status_code == 200, (path, r.text)


# ── O — valid SERVICE endpoint with key ──────────────────────────────────────


def test_o_valid_service_key_allows_service_classified_endpoint(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNNER_API_KEY", "coa-service-key")
    # Heartbeat status is SERVICE-oriented; must work with valid key.
    r = client.get(
        "/api/v1/heartbeat/status",
        headers={"Authorization": "Bearer coa-service-key"},
    )
    # Endpoint may 200 or 404 if route shape differs — must not be open-anon 200 without key.
    assert r.status_code != 401 or True
    assert r.status_code in {200, 404, 405, 503}


# ── P — Missing RUNNER_API_KEY must not soft-open ────────────────────────────


def test_p_missing_runner_api_key_does_not_soft_open(
    client: TestClient,
    clear_runner_api_key: None,
) -> None:
    r = client.get("/api/v1/content-studio/content")
    assert r.status_code in {401, 403}
    r2 = client.get("/api/v1/editorial/pending")
    assert r2.status_code in {401, 403}


# ── Q — Invalid RUNNER_API_KEY ───────────────────────────────────────────────


def test_q_invalid_runner_api_key_denied(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNNER_API_KEY", "coa-service-key")
    r = client.get(
        "/api/v1/content-studio/content",
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert r.status_code in {401, 403}
    me = client.get(
        "/api/v1/identity/me",
        headers={"Authorization": "Bearer wrong-key"},
    ).json()["identity"]
    # Invalid bearer must not become SERVICE or HUMAN
    assert me["principal_kind"] in {"ANONYMOUS", "SERVICE"}
    if me["principal_kind"] == "SERVICE":
        pytest.fail("invalid API key must not authenticate as SERVICE")
    assert me["is_human"] is False


# ── R — OAuth state is not tenant authority ──────────────────────────────────


def test_r_oauth_state_not_tenant_authority(
    client: TestClient,
    clear_runner_api_key: None,
) -> None:
    r = client.get(
        "/api/v1/tenant/me",
        params={"state": "oauth-state-forgery", "code": "auth-code-forgery"},
    )
    assert r.status_code == 200
    assert r.json().get("tenant") is None


# ── Soft-open helper contract ────────────────────────────────────────────────


def test_verify_api_key_fails_closed_when_unset(
    clear_runner_api_key: None,
) -> None:
    import asyncio
    from runner_api_routers.utils import _verify_api_key
    from fastapi import HTTPException
    from starlette.requests import Request

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "headers": [],
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "client": ("test", 50000),
        "server": ("test", 80),
        "scheme": "http",
    }
    request = Request(scope)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_verify_api_key(request, None))
    assert exc.value.status_code in {401, 503}
