"""P0 Gmail tenant-safety — configure, OAuth, sync, heartbeat, contact, message-id."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

import revenue_os.models  # noqa: F401
import runner_api_routers.identity as identity_mod
import runner_api_routers.integrations as integrations_mod
from revenue_os.auth import hash_password
from revenue_os.config import settings
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
def gmail_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> sessionmaker:
    engine = create_engine(f"sqlite:///{tmp_path / 'gmail_tenant_safety.db'}")
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


def _seed_orgs(sf: sessionmaker, *, with_contacts: bool = False) -> None:
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
        if with_contacts:
            db.add_all(
                [
                    Contact(
                        id=_CONTACT_A,
                        first_name="A",
                        last_name="Shared",
                        email=_SHARED_EMAIL,
                        status=ContactStatus.LEAD,
                        source=ContactSource.MANUAL,
                        organization_id=_ORG_A,
                    ),
                    Contact(
                        id=_CONTACT_B,
                        first_name="B",
                        last_name="Shared",
                        email=_SHARED_EMAIL,
                        status=ContactStatus.LEAD,
                        source=ContactSource.MANUAL,
                        organization_id=_ORG_B,
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


def _activity_count(sf: sessionmaker, contact_id: uuid.UUID) -> int:
    db = sf()
    try:
        return (
            db.query(func.count(Activity.id))
            .filter(Activity.contact_id == contact_id)
            .scalar()
        )
    finally:
        db.close()


def _seed_gmail_oauth_clients(sf: sessionmaker) -> None:
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


def _fake_message(message_id: str, sender: str) -> dict:
    return {
        "id": message_id,
        "snippet": "reply body",
        "payload": {
            "headers": [
                {"name": "From", "value": f"Prospect <{sender}>"},
                {"name": "Subject", "value": "Re: hello"},
            ]
        },
    }


# ── A–E configure ────────────────────────────────────────────────────────────


def test_a_configure_trusted_org_a_writes_only_a(
    client: TestClient, gmail_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed_orgs(gmail_db)
    _login(client, email="owner-a@example.com", password="pass-o", org_id=str(_ORG_A))
    r = client.post("/api/v1/integrations/connectors/gmail/configure", json=_GMAIL_CONFIG)
    assert r.status_code == 200, r.text
    saved = _gmail_cfg(str(_ORG_A))
    assert saved is not None
    assert saved["client_id"] == "cid-a"
    assert _gmail_cfg(str(_ORG_B)) is None
    assert _null_org_gmail_count(gmail_db) == 0


def test_b_configure_api_key_only_fail_closed(
    client: TestClient, gmail_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_orgs(gmail_db)
    monkeypatch.setenv("RUNNER_API_KEY", "platform-only-key")
    r = client.post(
        "/api/v1/integrations/connectors/gmail/configure",
        json=_GMAIL_CONFIG,
        headers={"Authorization": "Bearer platform-only-key"},
    )
    assert r.status_code == 400
    assert "Organization context required" in r.text
    assert _gmail_cfg(str(_ORG_A)) is None
    assert _gmail_cfg(None) is None
    assert _null_org_gmail_count(gmail_db) == 0


def test_c_configure_body_org_b_session_a_cannot_write_b(
    client: TestClient, gmail_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed_orgs(gmail_db)
    _login(client, email="owner-a@example.com", password="pass-o", org_id=str(_ORG_A))
    r = client.post(
        "/api/v1/integrations/connectors/gmail/configure",
        json={**_GMAIL_CONFIG, "organization_id": str(_ORG_B)},
    )
    assert r.status_code == 200, r.text
    assert _gmail_cfg(str(_ORG_A)) is not None
    assert _gmail_cfg(str(_ORG_B)) is None


def test_d_configure_query_org_b_session_a_cannot_write_b(
    client: TestClient, gmail_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed_orgs(gmail_db)
    _login(client, email="owner-a@example.com", password="pass-o", org_id=str(_ORG_A))
    r = client.post(
        "/api/v1/integrations/connectors/gmail/configure",
        json=_GMAIL_CONFIG,
        params={"organization_id": str(_ORG_B)},
    )
    assert r.status_code == 200, r.text
    assert _gmail_cfg(str(_ORG_A)) is not None
    assert _gmail_cfg(str(_ORG_B)) is None


def test_e_configure_header_org_b_session_a_cannot_write_b(
    client: TestClient, gmail_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed_orgs(gmail_db)
    _login(client, email="owner-a@example.com", password="pass-o", org_id=str(_ORG_A))
    r = client.post(
        "/api/v1/integrations/connectors/gmail/configure",
        json=_GMAIL_CONFIG,
        headers={"X-Organization-Id": str(_ORG_B)},
    )
    assert r.status_code == 200, r.text
    assert _gmail_cfg(str(_ORG_A)) is not None
    assert _gmail_cfg(str(_ORG_B)) is None


# ── F–L OAuth ────────────────────────────────────────────────────────────────


def test_f_authorize_requires_tenant(client: TestClient, gmail_db) -> None:
    _seed_orgs(gmail_db)
    _seed_gmail_oauth_clients(gmail_db)
    r = client.get("/api/v1/integrations/gmail/authorize")
    # Anonymous callers fail closed at auth (401) or tenant gate (400).
    assert r.status_code in {400, 401}
    if r.status_code == 400:
        assert "Organization context required" in r.text


def test_g_authorize_state_is_signed_not_raw_uuid(
    client: TestClient, gmail_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed_orgs(gmail_db)
    _seed_gmail_oauth_clients(gmail_db)
    _login(client, email="owner-a@example.com", password="pass-o", org_id=str(_ORG_A))
    r = client.get("/api/v1/integrations/gmail/authorize")
    assert r.status_code == 200, r.text
    state = parse_qs(urlparse(r.json()["authorize_url"]).query)["state"][0]
    assert state != str(_ORG_A)
    payload = jwt.decode(state, settings.SECRET_KEY, algorithms=["HS256"])
    assert payload["purpose"] == "gmail_oauth"
    assert payload["organization_id"] == str(_ORG_A)


def test_h_callback_a_state_a_session_allowed(
    client: TestClient, gmail_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed_orgs(gmail_db)
    _seed_gmail_oauth_clients(gmail_db)
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync.exchange_code_for_tokens",
        lambda *a, **k: {"ok": True, "refresh_token": "oauth-token-a"},
    )
    _login(client, email="owner-a@example.com", password="pass-o", org_id=str(_ORG_A))
    auth = client.get("/api/v1/integrations/gmail/authorize")
    state = parse_qs(urlparse(auth.json()["authorize_url"]).query)["state"][0]
    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "auth-code-a", "state": state},
    )
    assert r.status_code == 200
    assert "connected successfully" in r.text.lower()
    assert _gmail_cfg(str(_ORG_A))["refresh_token"] == "oauth-token-a"
    assert _gmail_cfg(str(_ORG_B))["refresh_token"] == "refresh-b"


def test_i_callback_a_state_b_session_fail_closed(
    client: TestClient, gmail_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed_orgs(gmail_db)
    _seed_gmail_oauth_clients(gmail_db)
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync.exchange_code_for_tokens",
        lambda *a, **k: {"ok": True, "refresh_token": "stolen"},
    )
    _login(client, email="owner-a@example.com", password="pass-o", org_id=str(_ORG_A))
    auth = client.get("/api/v1/integrations/gmail/authorize")
    state_a = parse_qs(urlparse(auth.json()["authorize_url"]).query)["state"][0]
    client.cookies.clear()
    _login(client, email="owner-b@example.com", password="pass-b", org_id=str(_ORG_B))
    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "auth-code", "state": state_a},
    )
    assert "Organization context required" in r.text
    assert _gmail_cfg(str(_ORG_A))["refresh_token"] == "refresh-a"
    assert _gmail_cfg(str(_ORG_B))["refresh_token"] == "refresh-b"


def test_j_callback_valid_state_no_session_fail_closed(
    client: TestClient, gmail_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_orgs(gmail_db)
    _seed_gmail_oauth_clients(gmail_db)
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync.exchange_code_for_tokens",
        lambda *a, **k: {"ok": True, "refresh_token": "sessionless"},
    )
    state = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "auth-code", "state": state},
    )
    assert "Organization context required" in r.text
    assert _gmail_cfg(str(_ORG_A))["refresh_token"] == "refresh-a"
    assert _null_org_gmail_count(gmail_db) == 0


def test_k_malformed_and_expired_state_fail_closed(
    client: TestClient, gmail_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed_orgs(gmail_db)
    _seed_gmail_oauth_clients(gmail_db)
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync.exchange_code_for_tokens",
        lambda *a, **k: {"ok": True, "refresh_token": "bad-state"},
    )
    _login(client, email="owner-a@example.com", password="pass-o", org_id=str(_ORG_A))
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    expired = jwt.encode(
        {
            "purpose": "gmail_oauth",
            "organization_id": str(_ORG_A),
            "nonce": str(uuid.uuid4()),
            "iat": past,
            "exp": past + timedelta(minutes=1),
        },
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    for bad in ("not-a-jwt", str(_ORG_A), "eyJhbGciOiJub25lIn0.e30.", expired):
        r = client.get(
            "/api/v1/integrations/gmail/callback",
            params={"code": "auth-code", "state": bad},
        )
        assert "Organization context required" in r.text
    assert _gmail_cfg(str(_ORG_A))["refresh_token"] == "refresh-a"


def test_l_null_org_gmail_persistence_impossible(
    client: TestClient, gmail_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_orgs(gmail_db)
    save_calls: list[dict] = []

    def _capture(name, category, config, *, organization_id=None):  # noqa: ANN001
        save_calls.append({"name": name, "organization_id": organization_id})

    monkeypatch.setattr(
        "revenue_os.services.credentials_vault.save_credentials", _capture
    )
    r_cfg = client.post(
        "/api/v1/integrations/connectors/gmail/configure", json=_GMAIL_CONFIG
    )
    r_cb = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "x", "state": integrations_mod._sign_gmail_oauth_state(str(_ORG_A))},
    )
    assert r_cfg.status_code in {400, 401}
    assert "Organization context required" in r_cb.text or r_cb.status_code in {400, 401, 403}
    assert save_calls == []
    assert _null_org_gmail_count(gmail_db) == 0


# ── M–R sync / heartbeat / isolation ─────────────────────────────────────────


def test_m_manual_sync_a_only_a(
    client: TestClient, gmail_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed_orgs(gmail_db, with_contacts=True)
    _seed_gmail_oauth_clients(gmail_db)
    tokens: list[str] = []

    def _refresh(client_id, client_secret, refresh_token):  # noqa: ANN001
        tokens.append(refresh_token)
        return {"ok": True, "access_token": "tok"}

    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync._refresh_access_token", _refresh
    )
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync._list_message_ids",
        lambda *a, **k: ["msg-a"],
    )
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync._get_message",
        lambda token, mid: _fake_message(mid, _SHARED_EMAIL),
    )
    _login(client, email="owner-a@example.com", password="pass-o", org_id=str(_ORG_A))
    r = client.post("/api/v1/integrations/gmail/sync")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    assert body.get("organization_id") == str(_ORG_A)
    assert tokens == ["refresh-a"]
    assert _activity_count(gmail_db, _CONTACT_A) == 1
    assert _activity_count(gmail_db, _CONTACT_B) == 0


def test_n_q_r_heartbeat_legacy_and_missing_tenant(
    gmail_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    import revenue_os.integrations.gmail_sync as gs

    _seed_orgs(gmail_db, with_contacts=True)
    _seed_gmail_oauth_clients(gmail_db)
    save_credentials(
        "gmail",
        "email",
        {
            "client_id": "cid-g",
            "client_secret": "sec-g",
            "redirect_uri": "http://localhost/g",
            "refresh_token": "refresh-global",
        },
        organization_id=None,
    )

    tokens: list[str] = []

    def _refresh(client_id, client_secret, refresh_token):  # noqa: ANN001
        tokens.append(refresh_token)
        return {"ok": True, "access_token": "tok"}

    monkeypatch.setattr(gs, "_refresh_access_token", _refresh)
    monkeypatch.setattr(gs, "_list_message_ids", lambda *a, **k: ["msg-hb"])
    monkeypatch.setattr(
        gs, "_get_message", lambda token, mid: _fake_message(mid, _SHARED_EMAIL)
    )

    result_a = gs.sync_inbox(organization_id=str(_ORG_A))
    assert result_a.get("ok") is True
    assert tokens == ["refresh-a"]
    assert _activity_count(gmail_db, _CONTACT_A) == 1
    assert _activity_count(gmail_db, _CONTACT_B) == 0

    missing = gs.sync_inbox(organization_id=None)
    assert missing.get("blocked") is True
    empty = gs.sync_inbox(organization_ids=[])
    assert empty.get("blocked") is True
    multi = gs.sync_inbox(organization_ids=[str(_ORG_A), str(_ORG_B)])
    assert multi.get("blocked") is True

    # R: global row still stored but not consumed for Org A (already used A's token).
    global_cfg = load_credentials("gmail", organization_id=None)
    assert global_cfg is not None
    assert global_cfg["refresh_token"] == "refresh-global"
    assert "refresh-global" not in tokens


def test_o_shared_email_a_b_isolation(
    gmail_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    import revenue_os.integrations.gmail_sync as gs

    _seed_orgs(gmail_db, with_contacts=True)
    _seed_gmail_oauth_clients(gmail_db)
    monkeypatch.setattr(
        gs, "_refresh_access_token", lambda *a, **k: {"ok": True, "access_token": "tok"}
    )
    monkeypatch.setattr(gs, "_list_message_ids", lambda *a, **k: ["msg-shared"])
    monkeypatch.setattr(
        gs, "_get_message", lambda token, mid: _fake_message(mid, _SHARED_EMAIL)
    )
    assert gs.sync_inbox(organization_id=str(_ORG_A)).get("created") == 1
    assert gs.sync_inbox(organization_id=str(_ORG_B)).get("created") == 1
    assert _activity_count(gmail_db, _CONTACT_A) == 1
    assert _activity_count(gmail_db, _CONTACT_B) == 1


def test_p_same_message_id_does_not_cross_suppress(
    gmail_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    import revenue_os.integrations.gmail_sync as gs

    _seed_orgs(gmail_db, with_contacts=True)
    _seed_gmail_oauth_clients(gmail_db)
    monkeypatch.setattr(
        gs, "_refresh_access_token", lambda *a, **k: {"ok": True, "access_token": "tok"}
    )
    monkeypatch.setattr(gs, "_list_message_ids", lambda *a, **k: ["gmail-msg-shared-001"])
    monkeypatch.setattr(
        gs,
        "_get_message",
        lambda token, mid: _fake_message(mid, _SHARED_EMAIL),
    )
    assert gs.sync_inbox(organization_id=str(_ORG_A)).get("created") == 1
    assert gs.sync_inbox(organization_id=str(_ORG_B)).get("created") == 1
    assert _activity_count(gmail_db, _CONTACT_A) == 1
    assert _activity_count(gmail_db, _CONTACT_B) == 1


def test_non_gmail_slack_null_org_preserved(
    client: TestClient, gmail_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_orgs(gmail_db)
    monkeypatch.setenv("RUNNER_API_KEY", "slack-service-key")
    r = client.post(
        "/api/v1/integrations/connectors/slack/configure",
        json={"webhook_url": "https://hooks.slack.com/services/global"},
        headers={"Authorization": "Bearer slack-service-key"},
    )
    assert r.status_code == 200, r.text
    slack = load_credentials("slack", organization_id=None)
    assert slack is not None
    assert slack["webhook_url"] == "https://hooks.slack.com/services/global"
    assert _null_org_gmail_count(gmail_db) == 0
