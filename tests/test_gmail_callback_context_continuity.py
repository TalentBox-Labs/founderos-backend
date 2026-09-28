"""Gmail OAuth callback HUMAN+tenant context continuity (R1–R9).

Models the staging failure mode where callback authority resolution depends on
process-local request ContextVar instead of the ASGI Request carrying the
Founder session cookies after Google → Founder redirect.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import revenue_os.models  # noqa: F401
import runner_api_routers.identity as identity_mod
import runner_api_routers.integrations as integrations_mod
import revenue_os.services.tenant_resolution as tr_mod
from revenue_os.auth import hash_password
from revenue_os.config import settings
from revenue_os.models.base import Base
from revenue_os.models.organization import (
    MembershipStatus,
    Organization,
    OrganizationMembership,
    OrganizationStatus,
)
from revenue_os.models.user import User
from revenue_os.services.credentials_vault import load_credentials, save_credentials
from revenue_os.services.tenant_resolution import ORGANIZATION_COOKIE
from runner_api import app

_ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_OPERATOR = "Krishna Founder"


@pytest.fixture(autouse=True)
def _reset_identity() -> None:
    identity_mod._revoked_jtis.clear()
    identity_mod._login_failures.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def continuity_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> sessionmaker:
    engine = create_engine(f"sqlite:///{tmp_path / 'gmail_callback_continuity.db'}")
    Base.metadata.create_all(bind=engine)
    sf = sessionmaker(bind=engine)

    import revenue_os.database as db_mod
    import revenue_os.services.credentials_vault as vault_mod

    monkeypatch.setattr(db_mod, "SessionLocal", sf)
    monkeypatch.setattr(tr_mod, "SessionLocal", sf)
    monkeypatch.setattr(vault_mod, "_vault_db", lambda: sf())
    monkeypatch.setattr(identity_mod, "SessionLocal", sf)
    monkeypatch.setattr(integrations_mod, "SessionLocal", sf)
    return sf


def _seed_one_org(sf: sessionmaker) -> None:
    db = sf()
    try:
        db.add(
            Organization(
                id=_ORG_A, name="A", slug="a", status=OrganizationStatus.ACTIVE
            )
        )
        db.add(
            User(
                email="owner-a@example.com",
                hashed_password=hash_password("pass-o"),
                full_name="Owner A",
                role="owner",
                is_active=1,
            )
        )
        db.flush()
        user_a = db.query(User).filter(User.email == "owner-a@example.com").one()
        db.add(
            OrganizationMembership(
                user_id=user_a.id,
                organization_id=_ORG_A,
                role="owner",
                status=MembershipStatus.ACTIVE,
            )
        )
        db.commit()
    finally:
        db.close()


def _seed_two_orgs_same_human(sf: sessionmaker) -> None:
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
                    full_name="Multi Owner",
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
    finally:
        db.close()


def _seed_zero_membership_human(sf: sessionmaker) -> None:
    db = sf()
    try:
        db.add(
            User(
                email="orphan@example.com",
                hashed_password=hash_password("pass-z"),
                full_name="Orphan User",
                role="owner",
                is_active=1,
            )
        )
        db.commit()
    finally:
        db.close()


def _seed_gmail_client(sf: sessionmaker, org_id: uuid.UUID = _ORG_A) -> None:
    save_credentials(
        "gmail",
        "email",
        {
            "client_id": "cid-a",
            "client_secret": "sec-a",
            "redirect_uri": "http://testserver/api/v1/integrations/gmail/callback",
        },
        organization_id=str(org_id),
    )


def _login(client: TestClient, *, email: str, password: str, org_id: str | None) -> None:
    r = client.post(
        "/api/v1/identity/login",
        json={"email": email, "password": password},
    )
    assert r.status_code == 200, r.text
    if org_id is not None:
        client.cookies.set(ORGANIZATION_COOKIE, org_id)


def _stub_exchange(monkeypatch: pytest.MonkeyPatch, token: str = "oauth-rt") -> None:
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync.exchange_code_for_tokens",
        lambda *a, **k: {"ok": True, "refresh_token": token},
    )


def _break_request_contextvar(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate ContextVar loss across OAuth redirect / worker boundary."""
    monkeypatch.setattr(identity_mod, "current_request", lambda: None)
    monkeypatch.setattr(tr_mod, "current_request", lambda: None)


# ── R1 — valid HUMAN roundtrip must survive ContextVar loss ───────────────────


def test_r1_valid_human_callback_survives_contextvar_loss(
    client: TestClient, continuity_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed_one_org(continuity_db)
    _seed_gmail_client(continuity_db)
    _stub_exchange(monkeypatch)
    _login(client, email="owner-a@example.com", password="pass-o", org_id=str(_ORG_A))
    auth = client.get("/api/v1/integrations/gmail/authorize")
    assert auth.status_code == 200, auth.text
    state = parse_qs(urlparse(auth.json()["authorize_url"]).query)["state"][0]

    _break_request_contextvar(monkeypatch)
    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "auth-code-a", "state": state},
    )
    assert r.status_code == 200
    assert "connected successfully" in r.text.lower(), r.text
    assert load_credentials("gmail", organization_id=str(_ORG_A))["refresh_token"] == (
        "oauth-rt"
    )


# ── R2 / R3 — no session ─────────────────────────────────────────────────────


def test_r2_callback_without_session_fail_closed(
    client: TestClient, continuity_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_one_org(continuity_db)
    _seed_gmail_client(continuity_db)
    _stub_exchange(monkeypatch)
    state = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "auth-code", "state": state},
    )
    assert "Organization context required" in r.text
    assert load_credentials("gmail", organization_id=str(_ORG_A)).get("refresh_token") in (
        None,
        "",
    )


def test_r3_valid_state_without_session_fail_closed(
    client: TestClient, continuity_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_r2_callback_without_session_fail_closed(client, continuity_db, monkeypatch)


# ── R4 — zero membership ─────────────────────────────────────────────────────


def test_r4_zero_membership_fail_closed(
    client: TestClient, continuity_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed_zero_membership_human(continuity_db)
    _seed_gmail_client(continuity_db)
    _stub_exchange(monkeypatch)
    _login(client, email="orphan@example.com", password="pass-z", org_id=None)
    state = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "auth-code", "state": state},
    )
    assert "Organization context required" in r.text


# ── R5 — multi-org / no selection ─────────────────────────────────────────────


def test_r5_multi_org_no_selection_fail_closed(
    client: TestClient, continuity_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed_two_orgs_same_human(continuity_db)
    _seed_gmail_client(continuity_db)
    _stub_exchange(monkeypatch)
    _login(client, email="multi@example.com", password="pass-m", org_id=None)
    if ORGANIZATION_COOKIE in client.cookies:
        client.cookies.delete(ORGANIZATION_COOKIE)
    state = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "auth-code", "state": state},
    )
    # Fail closed: either HTML org-context page or HTTP 403 select-required.
    assert (
        "Organization context required" in r.text
        or r.status_code in {403, 400}
    )
    saved = load_credentials("gmail", organization_id=str(_ORG_A)) or {}
    assert saved.get("refresh_token") in (None, "")


# ── R6 — forged state ────────────────────────────────────────────────────────


def test_r6_forged_state_fail_closed(
    client: TestClient, continuity_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed_one_org(continuity_db)
    _seed_gmail_client(continuity_db)
    _stub_exchange(monkeypatch, token="forged")
    _login(client, email="owner-a@example.com", password="pass-o", org_id=str(_ORG_A))
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
    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "auth-code", "state": forged},
    )
    assert "Organization context required" in r.text
    assert (load_credentials("gmail", organization_id=str(_ORG_A)) or {}).get(
        "refresh_token"
    ) in (None, "")


# ── R7 — SERVICE + valid state ───────────────────────────────────────────────


def test_r7_service_api_key_cannot_callback(
    client: TestClient, continuity_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_one_org(continuity_db)
    _seed_gmail_client(continuity_db)
    _stub_exchange(monkeypatch, token="service-rt")
    monkeypatch.setenv("RUNNER_API_KEY", "platform-only-key")
    state = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "auth-code", "state": state},
        headers={"Authorization": "Bearer platform-only-key"},
    )
    assert "Organization context required" in r.text
    assert (load_credentials("gmail", organization_id=str(_ORG_A)) or {}).get(
        "refresh_token"
    ) in (None, "")


# ── R8 — client org assertions cannot establish tenant ───────────────────────


def test_r8_body_query_header_org_cannot_establish_tenant(
    client: TestClient, continuity_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_one_org(continuity_db)
    _seed_gmail_client(continuity_db)
    _stub_exchange(monkeypatch, token="client-org")
    state = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    for kwargs in (
        {"params": {"code": "c", "state": state, "organization_id": str(_ORG_A)}},
        {
            "params": {"code": "c", "state": state},
            "headers": {"X-Organization-Id": str(_ORG_A)},
        },
    ):
        r = client.get("/api/v1/integrations/gmail/callback", **kwargs)
        assert "Organization context required" in r.text
    assert (load_credentials("gmail", organization_id=str(_ORG_A)) or {}).get(
        "refresh_token"
    ) in (None, "")


def test_r8_state_alone_is_not_tenant_authority(
    client: TestClient, continuity_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F — OAuth state must not grant tenant persist without HUMAN session."""
    _seed_one_org(continuity_db)
    _seed_gmail_client(continuity_db)
    _stub_exchange(monkeypatch, token="state-only")
    state = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "c", "state": state},
    )
    assert "Organization context required" in r.text
    assert (load_credentials("gmail", organization_id=str(_ORG_A)) or {}).get(
        "refresh_token"
    ) in (None, "")


# ── R9 — restart / ContextVar boundary ───────────────────────────────────────


def test_r9_restart_boundary_contextvar_cleared_request_cookies_remain(
    client: TestClient, continuity_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Authorize under bound context; callback with ContextVar cleared (restart model)."""
    test_r1_valid_human_callback_survives_contextvar_loss(
        client, continuity_db, monkeypatch
    )


def test_callback_rejects_state_org_mismatch_even_with_request_di(
    client: TestClient, continuity_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed_one_org(continuity_db)
    _seed_gmail_client(continuity_db)
    _stub_exchange(monkeypatch, token="mismatch")
    _login(client, email="owner-a@example.com", password="pass-o", org_id=str(_ORG_A))
    foreign_state = integrations_mod._sign_gmail_oauth_state(str(_ORG_B))
    _break_request_contextvar(monkeypatch)
    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "c", "state": foreign_state},
    )
    assert "Organization context required" in r.text
    assert (load_credentials("gmail", organization_id=str(_ORG_A)) or {}).get(
        "refresh_token"
    ) in (None, "")
