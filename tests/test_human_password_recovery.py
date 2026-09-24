"""Operator HUMAN password recovery — adversarial authority and session epoch."""

from __future__ import annotations

import logging
import os
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
from revenue_os.auth import hash_password, verify_password
from revenue_os.config import settings
from revenue_os.db_url import validate_recovery_secret
from revenue_os.models.base import Base
from revenue_os.models.organization import (
    MembershipStatus,
    Organization,
    OrganizationMembership,
    OrganizationStatus,
)
from revenue_os.models.user import User
from revenue_os.services.human_password_recovery import (
    ENV_RECOVERY_SECRET,
    RecoveryOutcome,
    recover_human_password,
)
from runner_api import app

import importlib.util

_cli_path = Path(__file__).resolve().parents[1] / "scripts" / "reset_human_password.py"
_cli_spec = importlib.util.spec_from_file_location("reset_human_password_cli", _cli_path)
assert _cli_spec and _cli_spec.loader
recovery_cli = importlib.util.module_from_spec(_cli_spec)
_cli_spec.loader.exec_module(recovery_cli)
_EMAIL = "recover-owner@example.com"
_PASSWORD = "correct-horse-battery"
_NEW_PASSWORD = "recovered-horse-battery-xx"
_WEAK_PASSWORD = "short"
_NAME = "Recovery Owner"
_OTHER_EMAIL = "recover-other@example.com"
_OTHER_PASSWORD = "other-horse-battery-correct"
_OTHER_NAME = "Other Recovery"
_ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_RECOVERY_SECRET = "test-recovery-secret-not-for-production-use-xx"


@pytest.fixture(autouse=True)
def _reset_identity_ephemeral(monkeypatch: pytest.MonkeyPatch) -> None:
    identity_mod._revoked_jtis.clear()
    identity_mod._login_failures.clear()
    monkeypatch.setenv(ENV_RECOVERY_SECRET, _RECOVERY_SECRET)
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-not-for-production-use-only-in-ci")


@pytest.fixture()
def rec_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sessionmaker:
    db_path = tmp_path / "recover.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(identity_mod, "SessionLocal", factory)
    monkeypatch.setattr(tenant_resolution_mod, "SessionLocal", factory)
    return factory


@pytest.fixture()
def client(rec_db: sessionmaker) -> TestClient:
    return TestClient(app)


def _add_owner(
    factory: sessionmaker,
    *,
    email: str,
    password: str,
    name: str,
    role: str = "owner",
    org_id: uuid.UUID | None = _ORG_A,
    org_slug: str = "recover-org-a",
) -> User:
    db = factory()
    try:
        user = User(
            email=email,
            hashed_password=hash_password(password),
            full_name=name,
            role=role,
            is_active=1,
            token_version=0,
        )
        db.add(user)
        db.flush()
        if org_id is not None:
            org = db.get(Organization, org_id)
            if org is None:
                db.add(
                    Organization(
                        id=org_id,
                        name=f"Org {org_slug}",
                        slug=org_slug,
                        status=OrganizationStatus.ACTIVE,
                    )
                )
                db.flush()
            db.add(
                OrganizationMembership(
                    organization_id=org_id,
                    user_id=user.id,
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


@pytest.fixture()
def owner_user(rec_db: sessionmaker) -> User:
    return _add_owner(rec_db, email=_EMAIL, password=_PASSWORD, name=_NAME)


@pytest.fixture()
def other_user(rec_db: sessionmaker) -> User:
    return _add_owner(
        rec_db,
        email=_OTHER_EMAIL,
        password=_OTHER_PASSWORD,
        name=_OTHER_NAME,
        org_id=None,
    )


def _login(client: TestClient, email: str = _EMAIL, password: str = _PASSWORD) -> None:
    r = client.post("/api/v1/identity/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text


def _membership_count(factory: sessionmaker, user_id: uuid.UUID) -> int:
    db = factory()
    try:
        return (
            db.query(OrganizationMembership)
            .filter(OrganizationMembership.user_id == user_id)
            .count()
        )
    finally:
        db.close()


def _user_row(factory: sessionmaker, user_id: uuid.UUID) -> User:
    db = factory()
    try:
        user = db.get(User, user_id)
        assert user is not None
        db.expunge(user)
        return user
    finally:
        db.close()


def test_validate_recovery_secret_rejects_weak() -> None:
    with pytest.raises(ValueError):
        validate_recovery_secret("short")
    with pytest.raises(ValueError):
        validate_recovery_secret("password")
    assert validate_recovery_secret(_RECOVERY_SECRET) == _RECOVERY_SECRET


def test_a_authorized_recovery_succeeds(
    rec_db: sessionmaker, owner_user: User, client: TestClient
) -> None:
    before_memberships = _membership_count(rec_db, owner_user.id)
    result = recover_human_password(
        email=_EMAIL,
        new_password=_NEW_PASSWORD,
        recovery_secret=_RECOVERY_SECRET,
        confirm_operator_recovery=True,
        session_factory=rec_db,
    )
    assert result.ok
    assert result.outcome is RecoveryOutcome.SUCCESS
    assert result.user_id == str(owner_user.id)
    assert result.token_version == 1

    row = _user_row(rec_db, owner_user.id)
    assert row.email == _EMAIL
    assert row.role == "owner"
    assert row.full_name == _NAME
    assert int(row.is_active) == 1
    assert int(row.token_version) == 1
    assert verify_password(_NEW_PASSWORD, row.hashed_password)
    assert not verify_password(_PASSWORD, row.hashed_password)
    assert _membership_count(rec_db, owner_user.id) == before_memberships

    _login(client, password=_NEW_PASSWORD)
    me = client.get("/api/v1/identity/me").json()["identity"]
    assert me["principal_kind"] == "HUMAN"
    assert me["is_human"] is True
    assert me["email"] == _EMAIL

    tenant = client.get("/api/v1/tenant/me")
    assert tenant.status_code == 200
    body = tenant.json()
    # Single active membership resolves without client tenant assertion.
    assert body.get("tenant") is not None

def test_b_pre_recovery_sessions_rejected(
    rec_db: sessionmaker, owner_user: User, client: TestClient
) -> None:
    _login(client)
    stale = client.cookies.get(identity_mod.IDENTITY_COOKIE)
    assert stale
    other = TestClient(app)
    _login(other)
    other_token = other.cookies.get(identity_mod.IDENTITY_COOKIE)
    assert other_token

    result = recover_human_password(
        email=_EMAIL,
        new_password=_NEW_PASSWORD,
        recovery_secret=_RECOVERY_SECRET,
        confirm_operator_recovery=True,
        session_factory=rec_db,
    )
    assert result.ok

    client.cookies.set(identity_mod.IDENTITY_COOKIE, stale)
    me = client.get("/api/v1/identity/me").json()["identity"]
    assert me["principal_kind"] == "ANONYMOUS"

    other.cookies.set(identity_mod.IDENTITY_COOKIE, other_token)
    me2 = other.get("/api/v1/identity/me").json()["identity"]
    assert me2["principal_kind"] == "ANONYMOUS"


def test_c_self_service_password_still_requires_current(
    client: TestClient, owner_user: User
) -> None:
    _login(client)
    r = client.post(
        "/api/v1/identity/password",
        json={"current_password": "wrong-current-xx", "new_password": _NEW_PASSWORD},
    )
    assert r.status_code == 401


def test_d_anonymous_cannot_recover(
    rec_db: sessionmaker, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_RECOVERY_SECRET, raising=False)
    result = recover_human_password(
        email=_EMAIL,
        new_password=_NEW_PASSWORD,
        recovery_secret=None,
        confirm_operator_recovery=True,
        session_factory=rec_db,
    )
    assert result.outcome is RecoveryOutcome.MISSING_RECOVERY_SECRET
    assert verify_password(_PASSWORD, _user_row(rec_db, owner_user.id).hashed_password)


def test_e_service_api_key_is_not_recovery_authority(
    rec_db: sessionmaker, owner_user: User, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.setenv("RUNNER_API_KEY", "service-key-not-recovery-authority-xxxxx")
    # SERVICE auth works for service endpoints, but recovery ignores it entirely.
    result = recover_human_password(
        email=_EMAIL,
        new_password=_NEW_PASSWORD,
        recovery_secret="wrong-secret-not-matching-configured-value",
        confirm_operator_recovery=True,
        session_factory=rec_db,
    )
    assert result.outcome is RecoveryOutcome.RECOVERY_SECRET_MISMATCH
    # Prove SERVICE bearer does not unlock a recovery HTTP surface (none exists).
    r = client.post(
        "/api/v1/identity/password",
        headers={"Authorization": "Bearer service-key-not-recovery-authority-xxxxx"},
        json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
    )
    assert r.status_code in (401, 403)


def test_f_ordinary_human_cannot_recover_other(
    rec_db: sessionmaker, owner_user: User, other_user: User, client: TestClient
) -> None:
    _login(client)
    # Even with a HUMAN session, recovery without correct recovery secret fails.
    result = recover_human_password(
        email=_OTHER_EMAIL,
        new_password=_NEW_PASSWORD,
        recovery_secret="wrong-secret-not-matching-configured-value",
        confirm_operator_recovery=True,
        session_factory=rec_db,
    )
    assert result.outcome is RecoveryOutcome.RECOVERY_SECRET_MISMATCH
    assert verify_password(
        _OTHER_PASSWORD, _user_row(rec_db, other_user.id).hashed_password
    )


def test_g_wrong_and_missing_capability(
    rec_db: sessionmaker, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong = recover_human_password(
        email=_EMAIL,
        new_password=_NEW_PASSWORD,
        recovery_secret="definitely-wrong-recovery-secret-value-xx",
        confirm_operator_recovery=True,
        session_factory=rec_db,
    )
    assert wrong.outcome is RecoveryOutcome.RECOVERY_SECRET_MISMATCH

    monkeypatch.setenv(ENV_RECOVERY_SECRET, "short")
    weak = recover_human_password(
        email=_EMAIL,
        new_password=_NEW_PASSWORD,
        recovery_secret="short",
        confirm_operator_recovery=True,
        session_factory=rec_db,
    )
    assert weak.outcome is RecoveryOutcome.WEAK_RECOVERY_SECRET


def test_h_target_safety(
    rec_db: sessionmaker, owner_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = recover_human_password(
        email="missing@example.com",
        new_password=_NEW_PASSWORD,
        recovery_secret=_RECOVERY_SECRET,
        confirm_operator_recovery=True,
        session_factory=rec_db,
    )
    assert missing.outcome is RecoveryOutcome.TARGET_NOT_FOUND

    db = rec_db()
    try:
        user = db.get(User, owner_user.id)
        assert user is not None
        user.is_active = 0
        db.commit()
    finally:
        db.close()
    inactive = recover_human_password(
        email=_EMAIL,
        new_password=_NEW_PASSWORD,
        recovery_secret=_RECOVERY_SECRET,
        confirm_operator_recovery=True,
        session_factory=rec_db,
    )
    assert inactive.outcome is RecoveryOutcome.TARGET_INACTIVE

    db = rec_db()
    try:
        user = db.get(User, owner_user.id)
        assert user is not None
        user.is_active = 1
        user.full_name = "agent:hermes"
        db.commit()
    finally:
        db.close()
    not_human = recover_human_password(
        email=_EMAIL,
        new_password=_NEW_PASSWORD,
        recovery_secret=_RECOVERY_SECRET,
        confirm_operator_recovery=True,
        session_factory=rec_db,
    )
    assert not_human.outcome is RecoveryOutcome.TARGET_NOT_HUMAN


def test_i_weak_password_and_confirmation(
    rec_db: sessionmaker, owner_user: User
) -> None:
    weak = recover_human_password(
        email=_EMAIL,
        new_password=_WEAK_PASSWORD,
        recovery_secret=_RECOVERY_SECRET,
        confirm_operator_recovery=True,
        session_factory=rec_db,
    )
    assert weak.outcome is RecoveryOutcome.WEAK_NEW_PASSWORD

    unconfirmed = recover_human_password(
        email=_EMAIL,
        new_password=_NEW_PASSWORD,
        recovery_secret=_RECOVERY_SECRET,
        confirm_operator_recovery=False,
        session_factory=rec_db,
    )
    assert unconfirmed.outcome is RecoveryOutcome.MISSING_CONFIRMATION


def test_j_audit_does_not_leak_secrets(
    rec_db: sessionmaker, owner_user: User, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        result = recover_human_password(
            email=_EMAIL,
            new_password=_NEW_PASSWORD,
            recovery_secret=_RECOVERY_SECRET,
            confirm_operator_recovery=True,
            session_factory=rec_db,
        )
    assert result.ok
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert _NEW_PASSWORD not in joined
    assert _RECOVERY_SECRET not in joined
    assert _PASSWORD not in joined
    assert "hashed_password" not in joined.lower() or _NEW_PASSWORD not in joined


def test_k_cli_happy_path(
    rec_db: sessionmaker, owner_user: User, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recovery_cli, "SessionLocal", rec_db)
    pw_file = tmp_path / "new.secret"
    sec_file = tmp_path / "rec.secret"
    pw_file.write_text(_NEW_PASSWORD)
    sec_file.write_text(_RECOVERY_SECRET)
    os.chmod(pw_file, 0o600)
    os.chmod(sec_file, 0o600)
    rc = recovery_cli.main(
        [
            "--email",
            _EMAIL,
            "--new-password-file",
            str(pw_file),
            "--recovery-secret-file",
            str(sec_file),
            "--i-confirm-operator-recovery",
        ]
    )
    assert rc == 0
    assert verify_password(_NEW_PASSWORD, _user_row(rec_db, owner_user.id).hashed_password)


def test_l_org_assertions_irrelevant_to_recovery_authority(
    rec_db: sessionmaker, owner_user: User
) -> None:
    # Body/query/header org assertions are not parameters of recover_human_password.
    # Wrong secret still fails even if a caller *wishes* org assertion granted power.
    result = recover_human_password(
        email=_EMAIL,
        new_password=_NEW_PASSWORD,
        recovery_secret="org-assertion-does-not-help-xxxxxxxxxxxx",
        confirm_operator_recovery=True,
        session_factory=rec_db,
    )
    assert result.outcome is RecoveryOutcome.RECOVERY_SECRET_MISMATCH


def test_m_login_issues_matching_token_version(
    rec_db: sessionmaker, owner_user: User, client: TestClient
) -> None:
    recover_human_password(
        email=_EMAIL,
        new_password=_NEW_PASSWORD,
        recovery_secret=_RECOVERY_SECRET,
        confirm_operator_recovery=True,
        session_factory=rec_db,
    )
    _login(client, password=_NEW_PASSWORD)
    token = client.cookies.get(identity_mod.IDENTITY_COOKIE)
    assert token
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    assert payload.get("tv") == 1
    me = client.get("/api/v1/identity/me").json()["identity"]
    assert me["principal_kind"] == "HUMAN"
