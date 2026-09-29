"""Beta Gmail deferment: freeze operational surfaces without deleting Gmail."""

from __future__ import annotations

import os
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
from revenue_os.config import settings
from revenue_os.models.base import Base
from revenue_os.models.organization import (
    MembershipStatus,
    Organization,
    OrganizationMembership,
    OrganizationStatus,
)
from revenue_os.models.user import User
from revenue_os.services.gmail_beta_freeze import gmail_beta_enabled, gmail_beta_frozen
from revenue_os.services.tenant_resolution import ORGANIZATION_COOKIE
from runner_api_routers.identity import IDENTITY_COOKIE


@pytest.fixture()
def freeze_client(tmp_path, monkeypatch):
    """HUMAN + single-org session with Gmail beta frozen (default)."""
    monkeypatch.setenv("FOUNDER_OS_GMAIL_BETA_ENABLED", "0")
    monkeypatch.delenv("RUNNER_API_KEY", raising=False)
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", "Freeze Human")

    db_path = tmp_path / "gmail_beta_freeze.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)

    import revenue_os.database as db_mod
    import revenue_os.services.credentials_vault as vault_mod
    import revenue_os.services.tenant_resolution as tr_mod

    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    monkeypatch.setattr(tr_mod, "SessionLocal", Session)
    monkeypatch.setattr(vault_mod, "_vault_db", lambda: Session())
    monkeypatch.setattr(identity_mod, "SessionLocal", Session)
    monkeypatch.setattr(integrations_mod, "SessionLocal", Session)

    org_id = uuid.uuid4()
    db = Session()
    try:
        db.add(
            Organization(
                id=org_id,
                name="Freeze Org",
                slug=f"freeze-{org_id.hex[:8]}",
                status=OrganizationStatus.ACTIVE,
            )
        )
        db.add(
            User(
                email=f"freeze-{org_id.hex[:8]}@example.com",
                hashed_password=hash_password("FreezePass123!"),
                full_name="Freeze Human",
                role="owner",
                is_active=1,
            )
        )
        db.flush()
        user = (
            db.query(User)
            .filter(User.email == f"freeze-{org_id.hex[:8]}@example.com")
            .one()
        )
        db.add(
            OrganizationMembership(
                user_id=user.id,
                organization_id=org_id,
                role="owner",
                status=MembershipStatus.ACTIVE,
            )
        )
        db.commit()
        email = user.email
    finally:
        db.close()

    from runner_api import app

    client = TestClient(app)
    login = client.post(
        "/api/v1/identity/login",
        json={"email": email, "password": "FreezePass123!"},
    )
    assert login.status_code == 200, login.text
    client.cookies.set(ORGANIZATION_COOKIE, str(org_id))
    assert client.cookies.get(IDENTITY_COOKIE)
    return client, str(org_id)


def test_gmail_beta_default_frozen(monkeypatch):
    monkeypatch.delenv("FOUNDER_OS_GMAIL_BETA_ENABLED", raising=False)
    assert gmail_beta_frozen() is True
    assert gmail_beta_enabled() is False


def test_gmail_beta_explicit_enable(monkeypatch):
    monkeypatch.setenv("FOUNDER_OS_GMAIL_BETA_ENABLED", "1")
    assert gmail_beta_enabled() is True
    assert gmail_beta_frozen() is False


def test_connectors_omit_gmail_when_frozen(freeze_client):
    client, _org = freeze_client
    res = client.get("/api/v1/integrations/connectors")
    assert res.status_code == 200
    body = res.json()
    assert body.get("gmail_beta_enabled") is False
    names = [c["name"] for c in body["connectors"]]
    assert "gmail" not in names
    assert "email_smtp" in names


def test_authorize_direct_call_forbidden_when_frozen(freeze_client):
    client, _org = freeze_client
    res = client.get("/api/v1/integrations/gmail/authorize")
    assert res.status_code == 403
    assert "deferred" in res.json()["detail"].lower() or "beta" in res.json()["detail"].lower()


def test_authorize_navigate_forbidden_when_frozen(freeze_client):
    client, _org = freeze_client
    res = client.get(
        "/api/v1/integrations/gmail/authorize",
        headers={"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"},
    )
    assert res.status_code == 403


def test_sync_direct_call_forbidden_when_frozen(freeze_client):
    client, _org = freeze_client
    res = client.post("/api/v1/integrations/gmail/sync")
    assert res.status_code == 403


def test_configure_gmail_forbidden_when_frozen(freeze_client):
    client, _org = freeze_client
    res = client.post(
        "/api/v1/integrations/connectors/gmail/configure",
        json={
            "client_id": "x",
            "client_secret": "y",
            "redirect_uri": "https://example.invalid/callback",
        },
    )
    assert res.status_code == 403


def test_callback_route_retained_no_mutation_when_frozen(freeze_client, monkeypatch):
    client, _org = freeze_client
    exchanged = {"called": False}

    def _boom(*_a, **_k):
        exchanged["called"] = True
        return {"ok": True, "refresh_token": "should-not-save"}

    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync.exchange_code_for_tokens", _boom
    )
    res = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "not-a-real-code", "state": "not-a-real-state"},
    )
    assert res.status_code == 200
    assert "deferred" in res.text.lower() or "beta" in res.text.lower()
    assert exchanged["called"] is False


def test_heartbeat_gmail_job_isolated_not_executed(monkeypatch):
    monkeypatch.setenv("FOUNDER_OS_GMAIL_BETA_ENABLED", "0")
    from revenue_os.scheduler import job_sync_gmail_inbox

    out = job_sync_gmail_inbox()
    assert out.get("ok") is False
    assert out.get("executed") is False
    assert out.get("gmail_beta_enabled") is False


def test_authorize_allowed_when_explicitly_enabled(freeze_client, monkeypatch):
    """Reactivation path: enable flag restores authorize surface (still needs vault)."""
    client, _org = freeze_client
    monkeypatch.setenv("FOUNDER_OS_GMAIL_BETA_ENABLED", "1")
    res = client.get("/api/v1/integrations/gmail/authorize")
    # Not frozen: may 400 for missing oauth client config, never 403 freeze.
    assert res.status_code != 403
