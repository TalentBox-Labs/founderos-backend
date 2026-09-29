"""Credential-safe Gmail callback diagnostic classification tests.

Proves failure-stage enums and that diagnostic output never contains secrets.
Does not change OAuth/tenant authority semantics.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import revenue_os.models  # noqa: F401
import runner_api_routers.identity as identity_mod
import runner_api_routers.integrations as integrations_mod
import revenue_os.services.gmail_callback_diagnostics as diag_mod
import revenue_os.services.tenant_resolution as tr_mod
from revenue_os.auth import create_access_token, hash_password
from revenue_os.config import settings
from revenue_os.models.base import Base
from revenue_os.models.organization import (
    MembershipStatus,
    Organization,
    OrganizationMembership,
    OrganizationStatus,
)
from revenue_os.models.user import User
from revenue_os.services.credentials_vault import save_credentials
from revenue_os.services.gmail_callback_diagnostics import (
    diagnose_gmail_callback_authority,
    emit_gmail_callback_diagnostics,
    format_gmail_callback_diagnostic_line,
)
from revenue_os.services.identity_context import PrincipalKind
from revenue_os.services.session_revocation import revoke_jti
from revenue_os.services.tenant_resolution import ORGANIZATION_COOKIE
from runner_api import app
from runner_api_routers.identity import IDENTITY_COOKIE

_ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_ORG_C = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_OPERATOR = "Diag Owner"



@pytest.fixture(autouse=True)
def _enable_gmail_beta_for_operational_suite(monkeypatch):
    """Operational Gmail suites re-enable Gmail; beta default is frozen."""
    monkeypatch.setenv("FOUNDER_OS_GMAIL_BETA_ENABLED", "1")

@pytest.fixture(autouse=True)
def _reset_identity() -> None:
    identity_mod._revoked_jtis.clear()
    identity_mod._login_failures.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def diag_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> sessionmaker:
    engine = create_engine(f"sqlite:///{tmp_path / 'gmail_callback_diag.db'}")
    Base.metadata.create_all(bind=engine)
    sf = sessionmaker(bind=engine)

    import revenue_os.database as db_mod
    import revenue_os.services.credentials_vault as vault_mod

    monkeypatch.setattr(db_mod, "SessionLocal", sf)
    monkeypatch.setattr(tr_mod, "SessionLocal", sf)
    monkeypatch.setattr(vault_mod, "_vault_db", lambda: sf())
    monkeypatch.setattr(identity_mod, "SessionLocal", sf)
    monkeypatch.setattr(integrations_mod, "SessionLocal", sf)
    monkeypatch.setattr(diag_mod, "SessionLocal", sf)
    return sf


def _seed_one_org(sf: sessionmaker, *, full_name: str = _OPERATOR) -> User:
    db = sf()
    try:
        db.add(
            Organization(
                id=_ORG_A, name="A", slug="a", status=OrganizationStatus.ACTIVE
            )
        )
        db.add(
            User(
                email="diag-owner@example.com",
                hashed_password=hash_password("pass-o"),
                full_name=full_name,
                role="owner",
                is_active=1,
                token_version=0,
            )
        )
        db.flush()
        user = db.query(User).filter(User.email == "diag-owner@example.com").one()
        db.add(
            OrganizationMembership(
                user_id=user.id,
                organization_id=_ORG_A,
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


def _seed_two_orgs(sf: sessionmaker) -> User:
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
                User(
                    email="multi@example.com",
                    hashed_password=hash_password("pass-m"),
                    full_name=_OPERATOR,
                    role="owner",
                    is_active=1,
                ),
            ]
        )
        db.flush()
        user = db.query(User).filter(User.email == "multi@example.com").one()
        db.add_all(
            [
                OrganizationMembership(
                    user_id=user.id,
                    organization_id=_ORG_A,
                    role="owner",
                    status=MembershipStatus.ACTIVE,
                ),
                OrganizationMembership(
                    user_id=user.id,
                    organization_id=_ORG_B,
                    role="owner",
                    status=MembershipStatus.ACTIVE,
                ),
            ]
        )
        db.commit()
        db.refresh(user)
        db.expunge(user)
        return user
    finally:
        db.close()


def _seed_zero_membership(sf: sessionmaker) -> User:
    db = sf()
    try:
        db.add(
            User(
                email="orphan@example.com",
                hashed_password=hash_password("pass-z"),
                full_name=_OPERATOR,
                role="owner",
                is_active=1,
            )
        )
        db.commit()
        user = db.query(User).filter(User.email == "orphan@example.com").one()
        db.expunge(user)
        return user
    finally:
        db.close()


def _seed_gmail(sf: sessionmaker) -> None:
    save_credentials(
        "gmail",
        "email",
        {
            "client_id": "cid-a",
            "client_secret": "sec-a",
            "redirect_uri": "http://testserver/api/v1/integrations/gmail/callback",
        },
        organization_id=str(_ORG_A),
    )


def _token_for(
    user: User,
    *,
    tv: int | None = None,
    jti: str | None = None,
    kind: str = PrincipalKind.HUMAN.value,
) -> str:
    return create_access_token(
        {
            "sub": str(user.id),
            "email": user.email,
            "name": user.full_name,
            "role": "owner",
            "kind": kind,
            "amr": "password",
            "jti": jti or str(uuid.uuid4()),
            "tv": int(user.token_version if tv is None else tv),
            "iss": "founder_os_runner_api",
        }
    )


def _request_with_cookies(
    client: TestClient, cookies: dict[str, str]
) -> object:
    """Build a Starlette Request-like object via a callback hit's ASGI scope.

    Prefer diagnosing through TestClient callback + captured logs / direct
    Request construction from the app dependency path.
    """
    from starlette.requests import Request

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/integrations/gmail/callback",
        "raw_path": b"/api/v1/integrations/gmail/callback",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    # Cookie header
    if cookies:
        cookie_hdr = "; ".join(f"{k}={v}" for k, v in cookies.items())
        scope["headers"] = [(b"cookie", cookie_hdr.encode("latin-1"))]

    async def _receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, _receive)


def _assert_line_secret_free(line: str) -> None:
    forbidden_substrings = (
        "eyJ",  # JWT header prefix
        "cookie=",
        "client_secret",
        "sec-a",
        "pass-o",
        "pass-m",
        "postgresql://",
        "SECRET_KEY",
        "Bearer ",
        str(_ORG_A),
        str(_ORG_B),
        "authorization_code=",
        "refresh_token",
        "access_token",
    )
    lower = line.lower()
    for bad in forbidden_substrings:
        assert bad.lower() not in lower, f"diagnostic leaked {bad!r} in {line!r}"
    assert "gmail_callback.authority_diagnostic" in line
    assert "failure_stage=" in line


def test_missing_session_cookie(diag_db, client: TestClient) -> None:
    _seed_one_org(diag_db)
    req = _request_with_cookies(client, {})
    state = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    d = diagnose_gmail_callback_authority(req, state=state, code="auth-code")
    assert d["failure_stage"] == "NO_SESSION_COOKIE"
    assert d["session_cookie_present"] is False
    _assert_line_secret_free(format_gmail_callback_diagnostic_line(d))


def test_invalid_session(diag_db, client: TestClient) -> None:
    _seed_one_org(diag_db)
    req = _request_with_cookies(client, {IDENTITY_COOKIE: "not-a-jwt"})
    state = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    d = diagnose_gmail_callback_authority(req, state=state, code="auth-code")
    assert d["failure_stage"] == "INVALID_SESSION"
    assert d["session_cookie_present"] is True
    assert d["session_valid"] is False
    _assert_line_secret_free(format_gmail_callback_diagnostic_line(d))


def test_revoked_session(diag_db, client: TestClient) -> None:
    user = _seed_one_org(diag_db)
    jti = str(uuid.uuid4())
    token = _token_for(user, jti=jti)
    revoke_jti(
        jti,
        datetime.now(timezone.utc) + timedelta(hours=1),
        session_factory=diag_db,
    )
    req = _request_with_cookies(client, {IDENTITY_COOKIE: token})
    state = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    d = diagnose_gmail_callback_authority(req, state=state, code="auth-code")
    assert d["failure_stage"] == "REVOKED_SESSION"
    assert d["session_valid"] is True
    assert d["revocation_valid"] is False
    _assert_line_secret_free(format_gmail_callback_diagnostic_line(d))


def test_token_version_mismatch(diag_db, client: TestClient) -> None:
    user = _seed_one_org(diag_db)
    token = _token_for(user, tv=0)
    db = diag_db()
    try:
        row = db.query(User).filter(User.id == user.id).one()
        row.token_version = 7
        db.commit()
    finally:
        db.close()
    req = _request_with_cookies(client, {IDENTITY_COOKIE: token})
    state = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    d = diagnose_gmail_callback_authority(req, state=state, code="auth-code")
    assert d["failure_stage"] == "TOKEN_VERSION_MISMATCH"
    assert d["token_version_valid"] is False
    _assert_line_secret_free(format_gmail_callback_diagnostic_line(d))


def test_human_not_resolved(diag_db, client: TestClient) -> None:
    user = _seed_one_org(diag_db, full_name="ai:automation")
    token = _token_for(user)
    req = _request_with_cookies(client, {IDENTITY_COOKIE: token})
    state = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    d = diagnose_gmail_callback_authority(req, state=state, code="auth-code")
    assert d["failure_stage"] == "HUMAN_NOT_RESOLVED"
    assert d["identity_resolved"] is True
    assert d["is_human"] is False
    _assert_line_secret_free(format_gmail_callback_diagnostic_line(d))


def test_zero_membership(diag_db, client: TestClient) -> None:
    user = _seed_zero_membership(diag_db)
    token = _token_for(user)
    req = _request_with_cookies(client, {IDENTITY_COOKIE: token})
    state = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    d = diagnose_gmail_callback_authority(req, state=state, code="auth-code")
    assert d["failure_stage"] == "ZERO_MEMBERSHIP"
    assert d["membership_count"] == "0"
    _assert_line_secret_free(format_gmail_callback_diagnostic_line(d))


def test_multi_org_no_selection(diag_db, client: TestClient) -> None:
    user = _seed_two_orgs(diag_db)
    token = _token_for(user)
    req = _request_with_cookies(client, {IDENTITY_COOKIE: token})
    state = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    d = diagnose_gmail_callback_authority(req, state=state, code="auth-code")
    assert d["failure_stage"] == "MULTI_ORG_SELECTION_REQUIRED"
    assert d["membership_count"] == "many"
    _assert_line_secret_free(format_gmail_callback_diagnostic_line(d))


def test_selected_org_invalid(diag_db, client: TestClient) -> None:
    user = _seed_one_org(diag_db)
    token = _token_for(user)
    req = _request_with_cookies(
        client,
        {IDENTITY_COOKIE: token, ORGANIZATION_COOKIE: str(_ORG_C)},
    )
    state = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    d = diagnose_gmail_callback_authority(req, state=state, code="auth-code")
    assert d["failure_stage"] == "SELECTED_ORG_INVALID"
    assert d["selected_org_present"] is True
    _assert_line_secret_free(format_gmail_callback_diagnostic_line(d))


def test_tenant_resolution_failed(diag_db, client: TestClient, monkeypatch) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    user = _seed_one_org(diag_db)
    token = _token_for(user)

    class _Boom:
        def get(self, *a, **k):  # noqa: ANN001, ANN002
            return None

        def query(self, *a, **k):  # noqa: ANN001, ANN002
            raise SQLAlchemyError("boom")

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(diag_mod, "SessionLocal", lambda: _Boom())
    req = _request_with_cookies(
        client, {IDENTITY_COOKIE: token, ORGANIZATION_COOKIE: str(_ORG_A)}
    )
    state = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    d = diagnose_gmail_callback_authority(req, state=state, code="auth-code")
    assert d["failure_stage"] == "TENANT_RESOLUTION_FAILED"
    _assert_line_secret_free(format_gmail_callback_diagnostic_line(d))


def test_state_invalid(diag_db, client: TestClient) -> None:
    user = _seed_one_org(diag_db)
    token = _token_for(user)
    req = _request_with_cookies(
        client, {IDENTITY_COOKIE: token, ORGANIZATION_COOKIE: str(_ORG_A)}
    )
    forged = jwt.encode(
        {
            "purpose": "gmail_oauth",
            "organization_id": str(_ORG_A),
            "nonce": str(uuid.uuid4()),
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(minutes=15),
        },
        "wrong-secret-key-not-settings",
        algorithm="HS256",
    )
    d = diagnose_gmail_callback_authority(req, state=forged, code="auth-code")
    assert d["failure_stage"] == "STATE_INVALID"
    assert d["tenant_resolved"] is True
    assert d["state_present"] is True
    assert d["state_signature_valid"] is False
    _assert_line_secret_free(format_gmail_callback_diagnostic_line(d))


def test_state_correlation_failed(diag_db, client: TestClient) -> None:
    user = _seed_one_org(diag_db)
    token = _token_for(user)
    req = _request_with_cookies(
        client, {IDENTITY_COOKIE: token, ORGANIZATION_COOKIE: str(_ORG_A)}
    )
    foreign = integrations_mod._sign_gmail_oauth_state(str(_ORG_B))
    d = diagnose_gmail_callback_authority(req, state=foreign, code="auth-code")
    assert d["failure_stage"] == "STATE_CORRELATION_FAILED"
    assert d["state_signature_valid"] is True
    assert d["state_correlation_valid"] is False
    _assert_line_secret_free(format_gmail_callback_diagnostic_line(d))


def test_valid_human_tenant_state_success_path(
    diag_db, client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    user = _seed_one_org(diag_db)
    _seed_gmail(diag_db)
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync.exchange_code_for_tokens",
        lambda *a, **k: {"ok": True, "refresh_token": "oauth-rt"},
    )
    token = _token_for(user)
    client.cookies.set(IDENTITY_COOKIE, token)
    client.cookies.set(ORGANIZATION_COOKIE, str(_ORG_A))
    state = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))

    req = _request_with_cookies(
        client, {IDENTITY_COOKIE: token, ORGANIZATION_COOKIE: str(_ORG_A)}
    )
    d = diagnose_gmail_callback_authority(req, state=state, code="auth-code")
    assert d["failure_stage"] == "SUCCESS"
    assert d["tenant_resolved"] is True
    assert d["state_correlation_valid"] is True
    assert d["is_human"] is True

    with caplog.at_level(logging.INFO, logger=diag_mod.__name__):
        r = client.get(
            "/api/v1/integrations/gmail/callback",
            params={"code": "auth-code", "state": state},
        )
    assert r.status_code == 200
    assert "connected successfully" in r.text.lower()
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "gmail_callback.authority_diagnostic" in joined
    assert "gmail_callback.failure_stage=SUCCESS" in joined
    _assert_line_secret_free(joined)
    # Ensure raw state/code not echoed by diagnostic logger
    assert state not in joined
    assert "auth-code" not in joined
    assert token not in joined


def test_emit_strips_unexpected_secret_keys() -> None:
    dirty = {
        "failure_stage": "NO_SESSION_COOKIE",
        "cookie": "founder_os_identity=SECRETVALUE",
        "state": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig",
        "authorization_code": "4/0Axxx",
        "request_present": True,
        "session_cookie_present": False,
    }
    safe = emit_gmail_callback_diagnostics(dirty)
    assert "cookie" not in safe
    assert "state" not in safe
    assert "authorization_code" not in safe
    line = format_gmail_callback_diagnostic_line(safe)
    _assert_line_secret_free(line)
    assert "SECRETVALUE" not in line
    assert "eyJ" not in line
