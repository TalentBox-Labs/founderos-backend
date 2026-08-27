"""P0 adversarial — Gmail heartbeat/sync must never cross tenant boundaries."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

import revenue_os.models  # noqa: F401
import runner_api_routers.identity as identity_mod
import runner_api_routers.integrations as integrations_mod
from revenue_os.auth import hash_password
from revenue_os.models.activity import Activity, EmailActivity
from revenue_os.models.base import Base
from revenue_os.models.contact import Contact, ContactSource, ContactStatus
from revenue_os.models.integrations import ConnectorCredentialRecord
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
_CONTACT_A = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_CONTACT_B = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
_SHARED_EMAIL = "shared@example.com"
_MSG_ID = "gmail-msg-shared-001"
_OPERATOR = "Krishna Founder"


@pytest.fixture(autouse=True)
def _reset_identity() -> None:
    identity_mod._revoked_jtis.clear()
    identity_mod._login_failures.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def gmail_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> sessionmaker:
    engine = create_engine(f"sqlite:///{tmp_path / 'gmail_tenant.db'}")
    Base.metadata.create_all(bind=engine)
    sf = sessionmaker(bind=engine)

    import revenue_os.database as db_mod
    import revenue_os.integrations.gmail_sync as gmail_mod
    import revenue_os.services.activity_log as al_mod
    import revenue_os.services.credentials_vault as vault_mod
    import revenue_os.services.tenant_resolution as tr_mod

    monkeypatch.setattr(db_mod, "SessionLocal", sf)
    monkeypatch.setattr(tr_mod, "SessionLocal", sf)
    monkeypatch.setattr(gmail_mod, "SessionLocal", sf, raising=False)
    monkeypatch.setattr(al_mod, "SessionLocal", sf)
    monkeypatch.setattr(vault_mod, "_vault_db", lambda: sf())
    monkeypatch.setattr(identity_mod, "SessionLocal", sf)
    monkeypatch.setattr(integrations_mod, "SessionLocal", sf)
    return sf


def _seed_shared_email_tenants(sf: sessionmaker) -> None:
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
                Contact(
                    id=_CONTACT_A,
                    first_name="Shared",
                    last_name="A",
                    email=_SHARED_EMAIL,
                    status=ContactStatus.LEAD,
                    source=ContactSource.MANUAL,
                    organization_id=_ORG_A,
                    lead_score=0,
                ),
                Contact(
                    id=_CONTACT_B,
                    first_name="Shared",
                    last_name="B",
                    email=_SHARED_EMAIL,
                    status=ContactStatus.LEAD,
                    source=ContactSource.MANUAL,
                    organization_id=_ORG_B,
                    lead_score=0,
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    save_credentials(
        "gmail",
        "email",
        {
            "client_id": "cid-a",
            "client_secret": "sec-a",
            "redirect_uri": "http://localhost/a",
            "refresh_token": "refresh-a",
        },
        organization_id=str(_ORG_A),
    )
    save_credentials(
        "gmail",
        "email",
        {
            "client_id": "cid-b",
            "client_secret": "sec-b",
            "redirect_uri": "http://localhost/b",
            "refresh_token": "refresh-b",
        },
        organization_id=str(_ORG_B),
    )
    # Global/NULL-org bait — must never satisfy tenant sync.
    save_credentials(
        "gmail",
        "email",
        {
            "client_id": "cid-global",
            "client_secret": "sec-global",
            "redirect_uri": "http://localhost/g",
            "refresh_token": "refresh-global",
        },
        organization_id=None,
    )


def _gmail_message(from_header: str = f"Shared <{_SHARED_EMAIL}>") -> dict:
    return {
        "id": _MSG_ID,
        "snippet": "hello from shared sender",
        "payload": {
            "headers": [
                {"name": "From", "value": from_header},
                {"name": "Subject", "value": "Ping"},
            ]
        },
    }


def _patch_gmail_network(monkeypatch: pytest.MonkeyPatch, *, token_marker: list) -> None:
    import revenue_os.integrations.gmail_sync as gmail_mod

    def _refresh(client_id, client_secret, refresh_token):  # noqa: ANN001
        token_marker.append(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            }
        )
        return {"ok": True, "access_token": f"access-for-{refresh_token}"}

    monkeypatch.setattr(gmail_mod, "_refresh_access_token", _refresh)
    monkeypatch.setattr(gmail_mod, "_list_message_ids", lambda *_a, **_k: [_MSG_ID])
    monkeypatch.setattr(gmail_mod, "_get_message", lambda *_a, **_k: _gmail_message())


def _counts(sf: sessionmaker, contact_id: uuid.UUID) -> tuple[int, datetime | None]:
    db = sf()
    try:
        activities = (
            db.query(func.count(Activity.id))
            .filter(Activity.contact_id == contact_id)
            .scalar()
        )
        contact = db.get(Contact, contact_id)
        return int(activities or 0), contact.last_contacted_at if contact else None
    finally:
        db.close()


def test_org_a_sync_never_mutates_org_b(gmail_db, monkeypatch: pytest.MonkeyPatch) -> None:
    from revenue_os.integrations.gmail_sync import sync_inbox

    _seed_shared_email_tenants(gmail_db)
    tokens: list = []
    _patch_gmail_network(monkeypatch, token_marker=tokens)

    before_b_acts, before_b_lc = _counts(gmail_db, _CONTACT_B)
    result = sync_inbox(organization_id=str(_ORG_A))
    assert result.get("ok") is True
    assert result.get("created") == 1
    assert result.get("organization_id") == str(_ORG_A)

    assert tokens and tokens[0]["refresh_token"] == "refresh-a"
    assert tokens[0]["client_id"] == "cid-a"

    after_a_acts, after_a_lc = _counts(gmail_db, _CONTACT_A)
    after_b_acts, after_b_lc = _counts(gmail_db, _CONTACT_B)
    assert after_a_acts == 1
    assert after_a_lc is not None
    assert after_b_acts == before_b_acts == 0
    assert after_b_lc == before_b_lc is None

    db = gmail_db()
    try:
        ea = db.query(EmailActivity).one()
        act = db.get(Activity, ea.activity_id)
        assert act is not None
        assert act.contact_id == _CONTACT_A
        assert ea.message_id == _MSG_ID
    finally:
        db.close()


def test_org_b_sync_never_mutates_org_a(gmail_db, monkeypatch: pytest.MonkeyPatch) -> None:
    from revenue_os.integrations.gmail_sync import sync_inbox

    _seed_shared_email_tenants(gmail_db)
    tokens: list = []
    _patch_gmail_network(monkeypatch, token_marker=tokens)

    before_a_acts, before_a_lc = _counts(gmail_db, _CONTACT_A)
    result = sync_inbox(organization_id=str(_ORG_B))
    assert result.get("ok") is True
    assert result.get("created") == 1
    assert tokens[0]["refresh_token"] == "refresh-b"

    after_a_acts, after_a_lc = _counts(gmail_db, _CONTACT_A)
    after_b_acts, _ = _counts(gmail_db, _CONTACT_B)
    assert after_b_acts == 1
    assert after_a_acts == before_a_acts == 0
    assert after_a_lc == before_a_lc is None


def test_same_message_id_does_not_cross_suppress(gmail_db, monkeypatch: pytest.MonkeyPatch) -> None:
    from revenue_os.integrations.gmail_sync import sync_inbox

    _seed_shared_email_tenants(gmail_db)
    tokens: list = []
    _patch_gmail_network(monkeypatch, token_marker=tokens)

    assert sync_inbox(organization_id=str(_ORG_A)).get("created") == 1
    # Org B must still be able to attribute the same provider message_id.
    assert sync_inbox(organization_id=str(_ORG_B)).get("created") == 1

    db = gmail_db()
    try:
        rows = db.query(EmailActivity).filter(EmailActivity.message_id == _MSG_ID).all()
        assert len(rows) == 2
        contact_ids = {
            db.get(Activity, r.activity_id).contact_id for r in rows
        }
        assert contact_ids == {_CONTACT_A, _CONTACT_B}
    finally:
        db.close()


def test_missing_organization_fails_closed(gmail_db, monkeypatch: pytest.MonkeyPatch) -> None:
    from revenue_os.integrations.gmail_sync import sync_inbox

    _seed_shared_email_tenants(gmail_db)
    tokens: list = []
    _patch_gmail_network(monkeypatch, token_marker=tokens)

    assert sync_inbox().get("blocked") is True
    assert sync_inbox(organization_id=None).get("blocked") is True
    assert sync_inbox(organization_ids=[]).get("blocked") is True
    assert sync_inbox(organization_ids=[str(_ORG_A), str(_ORG_B)]).get("blocked") is True
    assert tokens == []
    assert _counts(gmail_db, _CONTACT_A)[0] == 0
    assert _counts(gmail_db, _CONTACT_B)[0] == 0


def test_tenant_credential_missing_ignores_global(
    gmail_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    from revenue_os.integrations.gmail_sync import sync_inbox
    from revenue_os.services.credentials_vault import delete_credentials

    _seed_shared_email_tenants(gmail_db)
    delete_credentials("gmail", organization_id=str(_ORG_A))
    assert load_credentials("gmail", organization_id=None) is not None
    assert (
        load_credentials(
            "gmail", organization_id=str(_ORG_A), allow_global_fallback=False
        )
        is None
    )

    tokens: list = []
    _patch_gmail_network(monkeypatch, token_marker=tokens)
    result = sync_inbox(organization_id=str(_ORG_A))
    assert result.get("ok") is False
    assert "not connected" in (result.get("reason") or "").lower()
    assert tokens == []
    assert _counts(gmail_db, _CONTACT_A)[0] == 0


def test_vault_config_never_loads_global_without_org(gmail_db) -> None:
    from revenue_os.integrations import gmail_sync as gmail_mod

    _seed_shared_email_tenants(gmail_db)
    src = Path(gmail_mod.__file__).read_text(encoding="utf-8")
    assert "allow_global_fallback=False" in src
    assert "organization_id=organization_id" in src
    # Must not call load_credentials with connector name alone.
    assert "load_credentials(CONNECTOR_NAME)" not in src
    cfg = gmail_mod._vault_config(str(_ORG_A))
    assert cfg is not None
    assert cfg["refresh_token"] == "refresh-a"
    assert gmail_mod._vault_config(str(uuid.uuid4())) is None


def test_manual_sync_binds_authenticated_org(
    client: TestClient, gmail_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed_shared_email_tenants(gmail_db)
    tokens: list = []
    _patch_gmail_network(monkeypatch, token_marker=tokens)

    # No tenant cookie / session → fail closed.
    r = client.post("/api/v1/integrations/gmail/sync")
    assert r.status_code == 400

    login = client.post(
        "/api/v1/identity/login",
        json={"email": "owner-a@example.com", "password": "pass-o"},
    )
    assert login.status_code == 200, login.text
    client.cookies.set(ORGANIZATION_COOKIE, str(_ORG_A))

    r = client.post("/api/v1/integrations/gmail/sync")
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert body.get("organization_id") == str(_ORG_A)
    assert tokens[-1]["refresh_token"] == "refresh-a"
    assert _counts(gmail_db, _CONTACT_A)[0] == 1
    assert _counts(gmail_db, _CONTACT_B)[0] == 0


def test_tenantless_oauth_callback_refuses_global_persist(
    client: TestClient, gmail_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_shared_email_tenants(gmail_db)

    monkeypatch.setattr(
        integrations_mod,
        "_integration_org_id",
        lambda: None,
    )
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync.exchange_code_for_tokens",
        lambda *a, **k: {"ok": True, "refresh_token": "stolen-refresh"},
    )

    db = gmail_db()
    try:
        before = (
            db.query(ConnectorCredentialRecord)
            .filter(
                ConnectorCredentialRecord.connector_name == "gmail",
                ConnectorCredentialRecord.organization_id.is_(None),
            )
            .count()
        )
    finally:
        db.close()

    r = client.get("/api/v1/integrations/gmail/callback", params={"code": "abc"})
    assert r.status_code == 200
    assert "Organization context required" in r.text

    db = gmail_db()
    try:
        after = (
            db.query(ConnectorCredentialRecord)
            .filter(
                ConnectorCredentialRecord.connector_name == "gmail",
                ConnectorCredentialRecord.organization_id.is_(None),
            )
            .count()
        )
        assert after == before
        global_cfg = load_credentials("gmail", organization_id=None)
        assert global_cfg is not None
        assert global_cfg.get("refresh_token") == "refresh-global"
    finally:
        db.close()


def test_oauth_callback_state_binds_org(
    client: TestClient, gmail_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_shared_email_tenants(gmail_db)
    monkeypatch.setattr(integrations_mod, "_integration_org_id", lambda: None)
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync.exchange_code_for_tokens",
        lambda *a, **k: {"ok": True, "refresh_token": "refresh-from-oauth-a"},
    )

    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "abc", "state": str(_ORG_A)},
    )
    assert r.status_code == 200
    assert "connected successfully" in r.text.lower()
    cfg = load_credentials(
        "gmail", organization_id=str(_ORG_A), allow_global_fallback=False
    )
    assert cfg is not None
    assert cfg["refresh_token"] == "refresh-from-oauth-a"
    # Org B untouched.
    cfg_b = load_credentials(
        "gmail", organization_id=str(_ORG_B), allow_global_fallback=False
    )
    assert cfg_b is not None
    assert cfg_b["refresh_token"] == "refresh-b"
