"""P1 adversarial — Gmail OAuth state must not grant tenant authority alone."""

from __future__ import annotations

import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import revenue_os.models  # noqa: F401
import runner_api_routers.identity as identity_mod
import runner_api_routers.integrations as integrations_mod
from revenue_os.auth import hash_password
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
def oauth_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> sessionmaker:
    engine = create_engine(f"sqlite:///{tmp_path / 'gmail_oauth.db'}")
    Base.metadata.create_all(bind=engine)
    sf = sessionmaker(bind=engine)

    import revenue_os.database as db_mod
    import revenue_os.services.credentials_vault as vault_mod
    import revenue_os.services.tenant_resolution as tr_mod

    monkeypatch.setattr(db_mod, "SessionLocal", sf)
    monkeypatch.setattr(tr_mod, "SessionLocal", sf)
    monkeypatch.setattr(vault_mod, "_vault_db", lambda: sf())
    monkeypatch.setattr(identity_mod, "SessionLocal", sf)
    monkeypatch.setattr(integrations_mod, "SessionLocal", sf)
    return sf


def _seed(sf: sessionmaker) -> None:
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
                    email="owner-a@example.com",
                    hashed_password=hash_password("pass-o"),
                    full_name="Owner A",
                    role="owner",
                    is_active=1,
                ),
                User(
                    email="owner-b@example.com",
                    hashed_password=hash_password("pass-b"),
                    full_name="Owner B",
                    role="owner",
                    is_active=1,
                ),
            ]
        )
        db.flush()
        user_a = db.query(User).filter(User.email == "owner-a@example.com").one()
        user_b = db.query(User).filter(User.email == "owner-b@example.com").one()
        db.add_all(
            [
                OrganizationMembership(
                    user_id=user_a.id,
                    organization_id=_ORG_A,
                    role="owner",
                    status=MembershipStatus.ACTIVE,
                ),
                OrganizationMembership(
                    user_id=user_b.id,
                    organization_id=_ORG_B,
                    role="owner",
                    status=MembershipStatus.ACTIVE,
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    for org, suffix in ((str(_ORG_A), "a"), (str(_ORG_B), "b")):
        save_credentials(
            "gmail",
            "email",
            {
                "client_id": f"cid-{suffix}",
                "client_secret": f"sec-{suffix}",
                "redirect_uri": f"http://localhost/{suffix}",
                "refresh_token": f"refresh-{suffix}",
            },
            organization_id=org,
        )


def _login(client: TestClient, *, email: str, password: str, org_id: str) -> None:
    r = client.post(
        "/api/v1/identity/login",
        json={"email": email, "password": password},
    )
    assert r.status_code == 200, r.text
    client.cookies.set(ORGANIZATION_COOKIE, org_id)


def _gmail_refresh(org_id: str) -> str | None:
    cfg = load_credentials(
        "gmail", organization_id=org_id, allow_global_fallback=False
    )
    return None if cfg is None else cfg.get("refresh_token")


def test_valid_org_a_oauth_persists_to_a(
    client: TestClient, oauth_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed(oauth_db)
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync.exchange_code_for_tokens",
        lambda *a, **k: {"ok": True, "refresh_token": "oauth-token-a"},
    )
    _login(
        client,
        email="owner-a@example.com",
        password="pass-o",
        org_id=str(_ORG_A),
    )
    auth = client.get("/api/v1/integrations/gmail/authorize")
    assert auth.status_code == 200, auth.text
    qs = parse_qs(urlparse(auth.json()["authorize_url"]).query)
    state = qs["state"][0]
    assert state != str(_ORG_A)

    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "auth-code-a", "state": state},
    )
    assert r.status_code == 200
    assert "connected successfully" in r.text.lower()
    assert _gmail_refresh(str(_ORG_A)) == "oauth-token-a"
    assert _gmail_refresh(str(_ORG_B)) == "refresh-b"


def test_state_tampered_a_to_b_rejected(
    client: TestClient, oauth_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed(oauth_db)
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync.exchange_code_for_tokens",
        lambda *a, **k: {"ok": True, "refresh_token": "should-not-land-on-b"},
    )
    _login(
        client,
        email="owner-a@example.com",
        password="pass-o",
        org_id=str(_ORG_A),
    )
    auth = client.get("/api/v1/integrations/gmail/authorize")
    assert auth.status_code == 200
    # Attacker replaces signed A state with Org B UUID (or B signed state).
    tampered = integrations_mod._sign_gmail_oauth_state(str(_ORG_B))
    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "auth-code", "state": tampered},
    )
    assert r.status_code == 200
    assert "Organization context required" in r.text
    assert _gmail_refresh(str(_ORG_A)) == "refresh-a"
    assert _gmail_refresh(str(_ORG_B)) == "refresh-b"


def test_state_session_mismatch_rejected(
    client: TestClient, oauth_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed(oauth_db)
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync.exchange_code_for_tokens",
        lambda *a, **k: {"ok": True, "refresh_token": "mismatch-token"},
    )
    state_a = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    _login(
        client,
        email="owner-b@example.com",
        password="pass-b",
        org_id=str(_ORG_B),
    )
    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "auth-code", "state": state_a},
    )
    assert r.status_code == 200
    assert "Organization context required" in r.text
    assert _gmail_refresh(str(_ORG_A)) == "refresh-a"
    assert _gmail_refresh(str(_ORG_B)) == "refresh-b"


def test_missing_state_rejected(
    client: TestClient, oauth_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed(oauth_db)
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync.exchange_code_for_tokens",
        lambda *a, **k: {"ok": True, "refresh_token": "no-state-token"},
    )
    _login(
        client,
        email="owner-a@example.com",
        password="pass-o",
        org_id=str(_ORG_A),
    )
    r = client.get("/api/v1/integrations/gmail/callback", params={"code": "auth-code"})
    assert r.status_code == 200
    assert "Organization context required" in r.text
    assert _gmail_refresh(str(_ORG_A)) == "refresh-a"


def test_malformed_state_rejected(
    client: TestClient, oauth_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed(oauth_db)
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync.exchange_code_for_tokens",
        lambda *a, **k: {"ok": True, "refresh_token": "bad-state-token"},
    )
    _login(
        client,
        email="owner-a@example.com",
        password="pass-o",
        org_id=str(_ORG_A),
    )
    for bad in ("not-a-jwt", str(_ORG_A), "eyJhbGciOiJub25lIn0.e30."):
        r = client.get(
            "/api/v1/integrations/gmail/callback",
            params={"code": "auth-code", "state": bad},
        )
        assert r.status_code == 200
        assert "Organization context required" in r.text
    assert _gmail_refresh(str(_ORG_A)) == "refresh-a"


def test_missing_tenant_session_fail_closed(
    client: TestClient, oauth_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(oauth_db)
    monkeypatch.setattr(integrations_mod, "_integration_org_id", lambda: None)
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync.exchange_code_for_tokens",
        lambda *a, **k: {"ok": True, "refresh_token": "sessionless-token"},
    )
    # Even a cryptographically valid signed state cannot authorize without session.
    state = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "auth-code", "state": state},
    )
    assert r.status_code == 200
    assert "Organization context required" in r.text
    assert _gmail_refresh(str(_ORG_A)) == "refresh-a"
    assert _gmail_refresh(str(_ORG_B)) == "refresh-b"


def test_null_org_never_persisted_from_oauth(
    client: TestClient, oauth_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(oauth_db)
    monkeypatch.setattr(integrations_mod, "_integration_org_id", lambda: None)
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync.exchange_code_for_tokens",
        lambda *a, **k: {"ok": True, "refresh_token": "global-poison"},
    )
    save_calls: list[dict] = []

    def _capture_save(name, category, config, *, organization_id=None):  # noqa: ANN001
        save_calls.append(
            {
                "name": name,
                "organization_id": organization_id,
                "refresh_token": config.get("refresh_token"),
            }
        )

    monkeypatch.setattr(
        "revenue_os.services.credentials_vault.save_credentials", _capture_save
    )
    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={
            "code": "auth-code",
            "state": integrations_mod._sign_gmail_oauth_state(str(_ORG_A)),
        },
    )
    assert r.status_code == 200
    assert save_calls == []
    assert "Organization context required" in r.text


def test_valid_org_b_oauth_independent_from_a(
    client: TestClient, oauth_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed(oauth_db)
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync.exchange_code_for_tokens",
        lambda *a, **k: {"ok": True, "refresh_token": "oauth-token-b"},
    )
    _login(
        client,
        email="owner-b@example.com",
        password="pass-b",
        org_id=str(_ORG_B),
    )
    auth = client.get("/api/v1/integrations/gmail/authorize")
    assert auth.status_code == 200
    state = parse_qs(urlparse(auth.json()["authorize_url"]).query)["state"][0]
    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "auth-code-b", "state": state},
    )
    assert r.status_code == 200
    assert "connected successfully" in r.text.lower()
    assert _gmail_refresh(str(_ORG_B)) == "oauth-token-b"
    assert _gmail_refresh(str(_ORG_A)) == "refresh-a"


def test_persist_helper_never_trusts_state_alone() -> None:
    """Direct contract: state without session yields None (fail closed)."""
    signed = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    # Patch session absent without HTTP stack.
    original = integrations_mod._integration_org_id
    try:
        integrations_mod._integration_org_id = lambda: None  # type: ignore[assignment]
        assert (
            integrations_mod._gmail_oauth_persist_organization_id(state=signed) is None
        )
        assert (
            integrations_mod._gmail_oauth_persist_organization_id(state=str(_ORG_A))
            is None
        )
        assert integrations_mod._gmail_oauth_persist_organization_id(state=None) is None
    finally:
        integrations_mod._integration_org_id = original  # type: ignore[assignment]
