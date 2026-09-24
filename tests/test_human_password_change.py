"""HUMAN self password-change — adversarial authority and bootstrap non-regression."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import revenue_os.models  # noqa: F401 — register tables
import revenue_os.services.tenant_resolution as tenant_resolution_mod
import runner_api_routers.identity as identity_mod
from revenue_os.auth import create_access_token, hash_password, verify_password
from revenue_os.config import settings
from revenue_os.models.base import Base
from revenue_os.models.organization import (
    MembershipStatus,
    Organization,
    OrganizationMembership,
    OrganizationStatus,
)
from revenue_os.models.session_revocation import SessionRevocation
from revenue_os.models.user import User
from revenue_os.services.identity_context import PrincipalKind
from revenue_os.services.session_revocation import SessionRevocationStoreUnavailable
from runner_api import app

_EMAIL = "pw-owner@example.com"
_PASSWORD = "correct-horse-battery"
_NEW_PASSWORD = "new-correct-horse-staple"
_WEAK_PASSWORD = "short"
_NAME = "Password Owner"
_OTHER_EMAIL = "pw-other@example.com"
_OTHER_PASSWORD = "other-horse-battery-correct"
_OTHER_NAME = "Other Owner"
_ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture(autouse=True)
def _reset_identity_ephemeral() -> None:
    identity_mod._revoked_jtis.clear()
    identity_mod._login_failures.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def pw_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> sessionmaker:
    engine = create_engine(f"sqlite:///{tmp_path / 'pw_change.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(identity_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(tenant_resolution_mod, "SessionLocal", session_factory)
    return session_factory


def _add_owner(
    db_factory: sessionmaker,
    *,
    email: str,
    password: str,
    name: str,
    org_id: uuid.UUID,
    org_slug: str,
) -> User:
    db = db_factory()
    try:
        user = User(
            email=email,
            hashed_password=hash_password(password),
            full_name=name,
            role="owner",
            is_active=1,
        )
        db.add(user)
        db.flush()
        org = Organization(
            id=org_id, name=f"Org {org_slug}", slug=org_slug, status=OrganizationStatus.ACTIVE
        )
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


@pytest.fixture
def owner_user(pw_db: sessionmaker) -> User:
    return _add_owner(
        pw_db,
        email=_EMAIL,
        password=_PASSWORD,
        name=_NAME,
        org_id=_ORG_A,
        org_slug="pw-org-a",
    )


@pytest.fixture
def other_user(pw_db: sessionmaker) -> User:
    return _add_owner(
        pw_db,
        email=_OTHER_EMAIL,
        password=_OTHER_PASSWORD,
        name=_OTHER_NAME,
        org_id=_ORG_B,
        org_slug="pw-org-b",
    )


def _login(client: TestClient, email: str = _EMAIL, password: str = _PASSWORD) -> None:
    r = client.post(
        "/api/v1/identity/login",
        json={"email": email, "password": password},
    )
    assert r.status_code == 200, r.text


def _cookie_token(client: TestClient) -> str:
    raw = client.cookies.get(identity_mod.IDENTITY_COOKIE)
    assert raw, "expected identity cookie"
    return raw


def _user_hash(db_factory: sessionmaker, user_id: uuid.UUID) -> str:
    db = db_factory()
    try:
        user = db.get(User, user_id)
        assert user is not None
        return user.hashed_password
    finally:
        db.close()


def test_a_anonymous_rejected(client: TestClient, owner_user: User) -> None:
    r = client.post(
        "/api/v1/identity/password",
        json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
    )
    assert r.status_code == 401


def test_b_service_api_key_rejected(
    client: TestClient, owner_user: User, monkeypatch: pytest.MonkeyPatch, pw_db: sessionmaker
) -> None:
    monkeypatch.setenv("RUNNER_API_KEY", "pw-service-key-only")
    before = _user_hash(pw_db, owner_user.id)
    r = client.post(
        "/api/v1/identity/password",
        json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
        headers={"Authorization": "Bearer pw-service-key-only"},
    )
    assert r.status_code == 401
    assert _user_hash(pw_db, owner_user.id) == before


def test_c_wrong_current_password_no_mutation(
    client: TestClient, owner_user: User, pw_db: sessionmaker
) -> None:
    _login(client)
    before = _user_hash(pw_db, owner_user.id)
    token = _cookie_token(client)
    r = client.post(
        "/api/v1/identity/password",
        json={"current_password": "wrong-current-password-xx", "new_password": _NEW_PASSWORD},
    )
    assert r.status_code == 401
    assert _user_hash(pw_db, owner_user.id) == before
    # Session still authoritative after failed attempt.
    client.cookies.set(identity_mod.IDENTITY_COOKIE, token)
    me = client.get("/api/v1/identity/me").json()["identity"]
    assert me["is_human"] is True


def test_d_valid_password_change(client: TestClient, owner_user: User, pw_db: sessionmaker) -> None:
    _login(client)
    r = client.post(
        "/api/v1/identity/password",
        json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
    )
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True}
    assert "password" not in body
    assert "hash" not in body
    assert "token" not in body
    db = pw_db()
    try:
        user = db.get(User, owner_user.id)
        assert user is not None
        assert verify_password(_NEW_PASSWORD, user.hashed_password)
        assert not verify_password(_PASSWORD, user.hashed_password)
    finally:
        db.close()


def test_e_old_password_login_rejected_after_change(
    client: TestClient, owner_user: User
) -> None:
    _login(client)
    assert (
        client.post(
            "/api/v1/identity/password",
            json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
        ).status_code
        == 200
    )
    r = client.post(
        "/api/v1/identity/login",
        json={"email": _EMAIL, "password": _PASSWORD},
    )
    assert r.status_code == 401


def test_f_new_password_login_accepted(client: TestClient, owner_user: User) -> None:
    _login(client)
    assert (
        client.post(
            "/api/v1/identity/password",
            json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
        ).status_code
        == 200
    )
    r = client.post(
        "/api/v1/identity/login",
        json={"email": _EMAIL, "password": _NEW_PASSWORD},
    )
    assert r.status_code == 200
    assert r.json()["identity"]["is_human"] is True


def test_g_current_session_revoked_after_change(
    client: TestClient, owner_user: User, pw_db: sessionmaker
) -> None:
    _login(client)
    token = _cookie_token(client)
    jti = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])["jti"]
    assert (
        client.post(
            "/api/v1/identity/password",
            json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
        ).status_code
        == 200
    )
    # Cookie cleared on success.
    assert client.cookies.get(identity_mod.IDENTITY_COOKIE) in (None, "")
    # Pre-change JWT must not remain authoritative.
    client.cookies.set(identity_mod.IDENTITY_COOKIE, token)
    me = client.get("/api/v1/identity/me").json()["identity"]
    assert me["principal_kind"] == "ANONYMOUS"
    db = pw_db()
    try:
        assert db.get(SessionRevocation, jti) is not None
    finally:
        db.close()


def test_h_other_session_revoked_via_token_version(
    client: TestClient, owner_user: User
) -> None:
    """Password change bumps token_version — other sessions lose HUMAN authority."""
    _login(client)
    other = TestClient(app)
    # Second independent session for same user.
    _login(other)
    other_token = _cookie_token(other)
    assert (
        client.post(
            "/api/v1/identity/password",
            json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
        ).status_code
        == 200
    )
    other.cookies.set(identity_mod.IDENTITY_COOKIE, other_token)
    me = other.get("/api/v1/identity/me").json()["identity"]
    assert me["principal_kind"] == "ANONYMOUS"
    assert me["is_human"] is False

def test_i_body_user_id_cannot_retarget(
    client: TestClient, owner_user: User, other_user: User, pw_db: sessionmaker
) -> None:
    _login(client)
    other_before = _user_hash(pw_db, other_user.id)
    r = client.post(
        "/api/v1/identity/password",
        json={
            "current_password": _PASSWORD,
            "new_password": _NEW_PASSWORD,
            "user_id": str(other_user.id),
        },
    )
    assert r.status_code == 200
    assert _user_hash(pw_db, other_user.id) == other_before
    assert verify_password(_NEW_PASSWORD, _user_hash(pw_db, owner_user.id))


def test_j_body_email_cannot_retarget(
    client: TestClient, owner_user: User, other_user: User, pw_db: sessionmaker
) -> None:
    _login(client)
    other_before = _user_hash(pw_db, other_user.id)
    r = client.post(
        "/api/v1/identity/password",
        json={
            "current_password": _PASSWORD,
            "new_password": _NEW_PASSWORD,
            "email": _OTHER_EMAIL,
        },
    )
    assert r.status_code == 200
    assert _user_hash(pw_db, other_user.id) == other_before


def test_k_query_assertion_cannot_target(
    client: TestClient, owner_user: User, other_user: User, pw_db: sessionmaker
) -> None:
    _login(client)
    other_before = _user_hash(pw_db, other_user.id)
    r = client.post(
        f"/api/v1/identity/password?user_id={other_user.id}&email={_OTHER_EMAIL}"
        f"&organization_id={_ORG_B}",
        json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
    )
    assert r.status_code == 200
    assert _user_hash(pw_db, other_user.id) == other_before


def test_l_header_assertion_cannot_target(
    client: TestClient, owner_user: User, other_user: User, pw_db: sessionmaker
) -> None:
    _login(client)
    other_before = _user_hash(pw_db, other_user.id)
    r = client.post(
        "/api/v1/identity/password",
        json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
        headers={
            "X-User-Id": str(other_user.id),
            "X-User-Email": _OTHER_EMAIL,
            "X-Organization-Id": str(_ORG_B),
        },
    )
    assert r.status_code == 200
    assert _user_hash(pw_db, other_user.id) == other_before


def test_m_service_plus_target_assertion_cannot_escalate(
    client: TestClient,
    owner_user: User,
    monkeypatch: pytest.MonkeyPatch,
    pw_db: sessionmaker,
) -> None:
    monkeypatch.setenv("RUNNER_API_KEY", "pw-service-key-escalate")
    before = _user_hash(pw_db, owner_user.id)
    r = client.post(
        "/api/v1/identity/password",
        json={
            "current_password": _PASSWORD,
            "new_password": _NEW_PASSWORD,
            "user_id": str(owner_user.id),
            "email": _EMAIL,
        },
        headers={
            "Authorization": "Bearer pw-service-key-escalate",
            "X-User-Id": str(owner_user.id),
        },
    )
    assert r.status_code == 401
    assert _user_hash(pw_db, owner_user.id) == before


def test_n_weak_new_password_rejected(
    client: TestClient, owner_user: User, pw_db: sessionmaker
) -> None:
    _login(client)
    before = _user_hash(pw_db, owner_user.id)
    for weak in (_WEAK_PASSWORD, "", "   ", "password", "changeme"):
        r = client.post(
            "/api/v1/identity/password",
            json={"current_password": _PASSWORD, "new_password": weak},
        )
        assert r.status_code in {400, 422}, weak
        assert _user_hash(pw_db, owner_user.id) == before


def test_o_db_failure_fails_closed(
    client: TestClient, owner_user: User, pw_db: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(client)
    before = _user_hash(pw_db, owner_user.id)
    real_factory = pw_db

    class _BoomSession:
        def __init__(self) -> None:
            self._inner = real_factory()

        def query(self, *a, **k):  # noqa: ANN001
            return self._inner.query(*a, **k)

        def commit(self) -> None:
            raise RuntimeError("simulated db commit failure")

        def rollback(self) -> None:
            self._inner.rollback()

        def close(self) -> None:
            self._inner.close()

        def __getattr__(self, name: str):  # noqa: ANN001
            return getattr(self._inner, name)

    monkeypatch.setattr(identity_mod, "SessionLocal", lambda: _BoomSession())
    r = client.post(
        "/api/v1/identity/password",
        json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
    )
    assert r.status_code == 503
    assert _user_hash(pw_db, owner_user.id) == before


def test_p_revocation_failure_fails_closed(
    client: TestClient, owner_user: User, pw_db: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(client)
    before = _user_hash(pw_db, owner_user.id)

    def _boom(*_a, **_k):  # noqa: ANN001
        raise SessionRevocationStoreUnavailable("simulated revocation failure")

    monkeypatch.setattr(identity_mod, "stage_jti_revocation", _boom)
    r = client.post(
        "/api/v1/identity/password",
        json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
    )
    assert r.status_code == 503
    assert _user_hash(pw_db, owner_user.id) == before
    # Cookie must still be usable because mutation failed closed.
    me = client.get("/api/v1/identity/me").json()["identity"]
    assert me["is_human"] is True


def test_q_password_absent_from_response_and_logs(
    client: TestClient, owner_user: User, caplog: pytest.LogCaptureFixture
) -> None:
    _login(client)
    with caplog.at_level(logging.INFO, logger="runner_api_routers.identity"):
        r = client.post(
            "/api/v1/identity/password",
            json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
        )
    assert r.status_code == 200
    payload = r.text
    assert _PASSWORD not in payload
    assert _NEW_PASSWORD not in payload
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert _PASSWORD not in joined
    assert _NEW_PASSWORD not in joined


def test_r_cross_tenant_human_cannot_retarget(
    client: TestClient, owner_user: User, other_user: User, pw_db: sessionmaker
) -> None:
    _login(client, email=_EMAIL, password=_PASSWORD)
    other_before = _user_hash(pw_db, other_user.id)
    # Authenticated as A; supply B credentials + target assertions.
    r = client.post(
        "/api/v1/identity/password",
        json={
            "current_password": _OTHER_PASSWORD,
            "new_password": _NEW_PASSWORD,
            "user_id": str(other_user.id),
            "email": _OTHER_EMAIL,
        },
    )
    # Wrong current password for authenticated principal A — reject, no mutation on B.
    assert r.status_code == 401
    assert _user_hash(pw_db, other_user.id) == other_before


def test_s_existing_bootstrap_user_rotatable(
    client: TestClient, pw_db: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    bootstrap_email = "bootstrap-pw@example.com"
    bootstrap_password = "bootstrap-initial-12"
    monkeypatch.setenv("FOUNDER_OS_BOOTSTRAP_EMAIL", bootstrap_email)
    monkeypatch.setenv("FOUNDER_OS_BOOTSTRAP_PASSWORD", bootstrap_password)
    monkeypatch.setenv("FOUNDER_OS_BOOTSTRAP_NAME", "Bootstrap Human")
    identity_mod.bootstrap_owner_if_needed()
    db = pw_db()
    try:
        user = db.query(User).filter(User.email == bootstrap_email).first()
        assert user is not None
        uid = user.id
    finally:
        db.close()
    _login(client, email=bootstrap_email, password=bootstrap_password)
    r = client.post(
        "/api/v1/identity/password",
        json={
            "current_password": bootstrap_password,
            "new_password": _NEW_PASSWORD,
        },
    )
    assert r.status_code == 200
    assert verify_password(_NEW_PASSWORD, _user_hash(pw_db, uid))


def test_t_bootstrap_startup_does_not_restore_password(
    client: TestClient, pw_db: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    bootstrap_email = "bootstrap-keep@example.com"
    old_bootstrap = "old-bootstrap-pass12"
    monkeypatch.setenv("FOUNDER_OS_BOOTSTRAP_EMAIL", bootstrap_email)
    monkeypatch.setenv("FOUNDER_OS_BOOTSTRAP_PASSWORD", old_bootstrap)
    monkeypatch.setenv("FOUNDER_OS_BOOTSTRAP_NAME", "Bootstrap Keep")
    identity_mod.bootstrap_owner_if_needed()
    _login(client, email=bootstrap_email, password=old_bootstrap)
    assert (
        client.post(
            "/api/v1/identity/password",
            json={"current_password": old_bootstrap, "new_password": _NEW_PASSWORD},
        ).status_code
        == 200
    )
    # Env still holds the old bootstrap secret; startup must not overwrite.
    monkeypatch.setenv("FOUNDER_OS_BOOTSTRAP_PASSWORD", old_bootstrap)
    identity_mod.bootstrap_owner_if_needed()
    db = pw_db()
    try:
        user = db.query(User).filter(User.email == bootstrap_email).first()
        assert user is not None
        assert verify_password(_NEW_PASSWORD, user.hashed_password)
        assert not verify_password(old_bootstrap, user.hashed_password)
    finally:
        db.close()
    # Login still requires the HUMAN-chosen password, not env bootstrap.
    assert (
        client.post(
            "/api/v1/identity/login",
            json={"email": bootstrap_email, "password": old_bootstrap},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/identity/login",
            json={"email": bootstrap_email, "password": _NEW_PASSWORD},
        ).status_code
        == 200
    )


def test_agent_kind_cookie_rejected(
    client: TestClient, owner_user: User, pw_db: sessionmaker
) -> None:
    before = _user_hash(pw_db, owner_user.id)
    token = create_access_token(
        {
            "sub": str(owner_user.id),
            "email": _EMAIL,
            "name": _NAME,
            "kind": PrincipalKind.AGENT.value,
            "jti": str(uuid.uuid4()),
        }
    )
    client.cookies.set(identity_mod.IDENTITY_COOKIE, token)
    r = client.post(
        "/api/v1/identity/password",
        json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
    )
    assert r.status_code == 401
    assert _user_hash(pw_db, owner_user.id) == before
