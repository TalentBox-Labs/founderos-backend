"""P1 adversarial — Gmail configure_connector requires trusted tenant org."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import revenue_os.models  # noqa: F401
import runner_api_routers.identity as identity_mod
import runner_api_routers.integrations as integrations_mod
from revenue_os.auth import hash_password
from revenue_os.models.base import Base
from revenue_os.models.integrations import ConnectorCredentialRecord
from revenue_os.models.organization import (
    MembershipStatus,
    Organization,
    OrganizationMembership,
    OrganizationStatus,
)
from revenue_os.models.user import User
from revenue_os.services.credentials_vault import load_credentials
from revenue_os.services.tenant_resolution import ORGANIZATION_COOKIE
from runner_api import app

_ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_OPERATOR = "Krishna Founder"

_GMAIL_CONFIG = {
    "client_id": "cid-a",
    "client_secret": "sec-a",
    "redirect_uri": "http://localhost/a",
}


@pytest.fixture(autouse=True)
def _reset_identity() -> None:
    identity_mod._revoked_jtis.clear()
    identity_mod._login_failures.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def configure_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> sessionmaker:
    engine = create_engine(f"sqlite:///{tmp_path / 'gmail_configure.db'}")
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


def _login(client: TestClient, *, email: str, password: str, org_id: str) -> None:
    r = client.post(
        "/api/v1/identity/login",
        json={"email": email, "password": password},
    )
    assert r.status_code == 200, r.text
    client.cookies.set(ORGANIZATION_COOKIE, org_id)


def _gmail_cfg(org_id: str | None) -> dict | None:
    return load_credentials(
        "gmail", organization_id=org_id, allow_global_fallback=False
    )


def _null_org_gmail_count(sf: sessionmaker) -> int:
    db = sf()
    try:
        return (
            db.query(ConnectorCredentialRecord)
            .filter(
                ConnectorCredentialRecord.connector_name == "gmail",
                ConnectorCredentialRecord.organization_id.is_(None),
            )
            .count()
        )
    finally:
        db.close()


def test_gmail_configure_trusted_org_a_persists_to_a(
    client: TestClient, configure_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed(configure_db)
    _login(
        client,
        email="owner-a@example.com",
        password="pass-o",
        org_id=str(_ORG_A),
    )
    r = client.post(
        "/api/v1/integrations/connectors/gmail/configure",
        json=_GMAIL_CONFIG,
    )
    assert r.status_code == 200, r.text
    saved = _gmail_cfg(str(_ORG_A))
    assert saved is not None
    assert saved["client_id"] == "cid-a"
    assert _gmail_cfg(str(_ORG_B)) is None
    assert _null_org_gmail_count(configure_db) == 0


def test_gmail_configure_missing_tenant_fail_closed(
    client: TestClient, configure_db
) -> None:
    _seed(configure_db)
    before = _null_org_gmail_count(configure_db)
    r = client.post(
        "/api/v1/integrations/connectors/gmail/configure",
        json=_GMAIL_CONFIG,
    )
    assert r.status_code == 400
    assert "Organization context required" in r.text
    assert _gmail_cfg(str(_ORG_A)) is None
    assert _gmail_cfg(str(_ORG_B)) is None
    assert _gmail_cfg(None) is None
    assert _null_org_gmail_count(configure_db) == before == 0


def test_gmail_configure_api_key_only_fail_closed(
    client: TestClient, configure_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(configure_db)
    monkeypatch.setenv("RUNNER_API_KEY", "platform-only-key")
    before = _null_org_gmail_count(configure_db)
    r = client.post(
        "/api/v1/integrations/connectors/gmail/configure",
        json={**_GMAIL_CONFIG, "organization_id": str(_ORG_A)},
        headers={
            "Authorization": "Bearer platform-only-key",
            "X-Organization-Id": str(_ORG_A),
        },
        params={"organization_id": str(_ORG_A)},
    )
    assert r.status_code == 400
    assert "Organization context required" in r.text
    assert _gmail_cfg(str(_ORG_A)) is None
    assert _gmail_cfg(None) is None
    assert _null_org_gmail_count(configure_db) == before == 0


def test_gmail_configure_org_a_cannot_persist_to_org_b(
    client: TestClient, configure_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed(configure_db)
    _login(
        client,
        email="owner-a@example.com",
        password="pass-o",
        org_id=str(_ORG_A),
    )
    r = client.post(
        "/api/v1/integrations/connectors/gmail/configure",
        json={**_GMAIL_CONFIG, "organization_id": str(_ORG_B)},
        headers={"X-Organization-Id": str(_ORG_B)},
        params={"organization_id": str(_ORG_B)},
    )
    assert r.status_code == 200, r.text
    saved_a = _gmail_cfg(str(_ORG_A))
    assert saved_a is not None
    assert saved_a["client_id"] == "cid-a"
    assert _gmail_cfg(str(_ORG_B)) is None
    assert _null_org_gmail_count(configure_db) == 0


def test_non_gmail_global_connector_null_org_preserved(
    client: TestClient, configure_db
) -> None:
    _seed(configure_db)
    r = client.post(
        "/api/v1/integrations/connectors/slack/configure",
        json={"webhook_url": "https://hooks.slack.com/services/global"},
    )
    assert r.status_code == 200, r.text
    slack_global = load_credentials("slack", organization_id=None)
    assert slack_global is not None
    assert slack_global["webhook_url"] == "https://hooks.slack.com/services/global"
    assert (
        load_credentials(
            "slack", organization_id=str(_ORG_A), allow_global_fallback=False
        )
        is None
    )
    assert _null_org_gmail_count(configure_db) == 0
