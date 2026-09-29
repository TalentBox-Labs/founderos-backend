"""RED/GREEN: Gmail authorize JSON state-pollution continuity defect.

Live Render access logs proved Google callbacks carried OAuth ``state`` values
polluted with a trailing JSON fragment from the authorize API response:

  <jwt>","organization_id":"<uuid>

Root mechanism: browser document navigation to ``/gmail/authorize`` rendered
JSON with ``authorize_url`` adjacent to ``organization_id``. Browser URL
linkification / copy-paste appended the next JSON field into ``state``. Google
echoed the polluted state; callback JWT verification failed closed.

At the first live Google return with this pollution class, the Founder HUMAN
session cookie WAS present (failure_stage would be STATE_INVALID). Later
cookieless callbacks were secondary. SameSite=Lax was NOT causal for the
connection failure.

This suite reproduces the pollution class on current contract semantics and
proves the browser-navigation redirect remediation emits a clean Location.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from jose import JWTError, jwt
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
from revenue_os.services.credentials_vault import save_credentials
from revenue_os.services.tenant_resolution import ORGANIZATION_COOKIE
from runner_api import app
from runner_api_routers.log_redaction import (
    redact_access_log_message,
    redact_query_string,
)

_ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_OPERATOR = "Krishna Founder"
_GMAIL_CONFIG = {
    "client_id": "cid-a",
    "client_secret": "sec-a",
    "redirect_uri": "https://founderos-staging.onrender.com/api/v1/integrations/gmail/callback",
}


@pytest.fixture(autouse=True)
def _reset_identity() -> None:
    identity_mod._revoked_jtis.clear()
    identity_mod._login_failures.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> sessionmaker:
    engine = create_engine(f"sqlite:///{tmp_path / 'gmail_oauth_integrity.db'}")
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
        user = db.query(User).filter(User.email == "owner-a@example.com").one()
        db.add(
            OrganizationMembership(
                user_id=user.id,
                organization_id=_ORG_A,
                role="owner",
                status=MembershipStatus.ACTIVE,
            )
        )
        db.commit()
    finally:
        db.close()
    save_credentials(
        "gmail",
        "email",
        _GMAIL_CONFIG,
        organization_id=str(_ORG_A),
    )


def _login(client: TestClient) -> None:
    r = client.post(
        "/api/v1/identity/login",
        json={"email": "owner-a@example.com", "password": "pass-o"},
    )
    assert r.status_code == 200, r.text
    client.cookies.set(ORGANIZATION_COOKIE, str(_ORG_A))


def _pollute_state_like_json_trailer(clean_state: str, org_id: str) -> str:
    """Reproduce the live browser-linkifier pollution class (synthetic only)."""
    return f'{clean_state}","organization_id":"{org_id}"'


def test_red_json_trailer_polluted_state_fails_with_session_present(
    client: TestClient, auth_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED: polluted state fails closed even when HUMAN session cookies exist."""
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed(auth_db)
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync.exchange_code_for_tokens",
        lambda *a, **k: {"ok": True, "refresh_token": "should-not-persist"},
    )
    _login(client)
    auth = client.get("/api/v1/integrations/gmail/authorize")
    assert auth.status_code == 200, auth.text
    clean_state = parse_qs(urlparse(auth.json()["authorize_url"]).query)["state"][0]
    polluted = _pollute_state_like_json_trailer(clean_state, str(_ORG_A))

    # Polluted token is not a valid JWT under our secret.
    with pytest.raises(JWTError):
        jwt.decode(polluted, settings.SECRET_KEY, algorithms=["HS256"])

    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "auth-code", "state": polluted},
    )
    assert r.status_code == 200
    assert "Organization context required" in r.text
    # Session cookies were sent (TestClient jar); failure is state correlation.
    assert client.cookies.get(identity_mod.IDENTITY_COOKIE)
    from revenue_os.services.credentials_vault import load_credentials

    cfg = load_credentials(
        "gmail", organization_id=str(_ORG_A), allow_global_fallback=False
    )
    assert cfg is not None
    assert cfg.get("refresh_token") != "should-not-persist"


def test_green_browser_navigation_authorize_redirects_clean_location(
    client: TestClient, auth_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GREEN: Sec-Fetch-Mode navigate → 302 Location with unpolluted state."""
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed(auth_db)
    _login(client)
    r = client.get(
        "/api/v1/integrations/gmail/authorize",
        headers={
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
            "Accept": "text/html,application/xhtml+xml",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    location = r.headers.get("location") or ""
    assert location.startswith("https://accounts.google.com/")
    assert '","organization_id"' not in location
    assert "%22,%22organization_id" not in location
    state = parse_qs(urlparse(location).query)["state"][0]
    payload = jwt.decode(state, settings.SECRET_KEY, algorithms=["HS256"])
    assert payload["purpose"] == "gmail_oauth"
    assert payload["organization_id"] == str(_ORG_A)


def test_api_client_authorize_still_returns_json(
    client: TestClient, auth_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """XHR/TestClient (no navigate fetch metadata) keep JSON authorize_url."""
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed(auth_db)
    _login(client)
    r = client.get("/api/v1/integrations/gmail/authorize")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["organization_id"] == str(_ORG_A)
    assert "authorize_url" in body
    # Hardening: organization_id must appear before authorize_url in serialized JSON
    # so residual linkifiers cannot append the next field into state.
    raw = r.content.decode()
    assert raw.index("organization_id") < raw.index("authorize_url")
    state = parse_qs(urlparse(body["authorize_url"]).query)["state"][0]
    jwt.decode(state, settings.SECRET_KEY, algorithms=["HS256"])


def test_callback_clean_state_with_session_succeeds(
    client: TestClient, auth_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed(auth_db)
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync.exchange_code_for_tokens",
        lambda *a, **k: {"ok": True, "refresh_token": "oauth-token-clean"},
    )
    _login(client)
    auth = client.get("/api/v1/integrations/gmail/authorize")
    state = parse_qs(urlparse(auth.json()["authorize_url"]).query)["state"][0]
    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "auth-code-clean", "state": state},
    )
    assert r.status_code == 200
    assert "connected successfully" in r.text.lower()
    from revenue_os.services.credentials_vault import load_credentials

    cfg = load_credentials(
        "gmail", organization_id=str(_ORG_A), allow_global_fallback=False
    )
    assert cfg is not None
    assert cfg.get("refresh_token") == "oauth-token-clean"


def test_callback_without_session_still_fail_closed(
    client: TestClient, auth_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(auth_db)
    monkeypatch.setattr(
        "revenue_os.integrations.gmail_sync.exchange_code_for_tokens",
        lambda *a, **k: {"ok": True, "refresh_token": "nope"},
    )
    state = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    r = client.get(
        "/api/v1/integrations/gmail/callback",
        params={"code": "auth-code", "state": state},
    )
    assert "Organization context required" in r.text


def test_query_string_redaction_masks_oauth_secrets() -> None:
    raw = (
        "code=4/0AXlqoSYNTHETIC&state=eyJhbGciOiJIUzI1NiJ9.SYNTH.SIG"
        "&iss=https://accounts.google.com"
    )
    redacted = redact_query_string(raw)
    assert "4/0AXlqoSYNTHETIC" not in redacted
    assert "eyJhbGciOiJIUzI1NiJ9.SYNTH.SIG" not in redacted
    assert "REDACTED" in redacted
    assert "accounts.google.com" in redacted

    access = (
        '10.0.0.1:0 - "GET /api/v1/integrations/gmail/callback?'
        'state=SECRETSTATE&code=SECRETCODE HTTP/1.1" 200 OK'
    )
    safe = redact_access_log_message(access)
    assert "SECRETSTATE" not in safe
    assert "SECRETCODE" not in safe
    assert "REDACTED" in safe


def test_structured_middleware_does_not_log_raw_oauth_query(
    client: TestClient, auth_db, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", _OPERATOR)
    _seed(auth_db)
    _login(client)
    state = integrations_mod._sign_gmail_oauth_state(str(_ORG_A))
    marker = "UNIQUE_OAUTH_STATE_MARKER_FOR_LOG_TEST"
    # Force a recognizable state value into the request query.
    with caplog.at_level(logging.INFO, logger="runner_api_routers.middleware"):
        client.get(
            "/api/v1/integrations/gmail/callback",
            params={"code": "UNIQUE_OAUTH_CODE_MARKER", "state": marker},
        )
    joined = " ".join(r.message for r in caplog.records)
    # Message line is method/path/status only; sensitive values live in extras.
    assert "UNIQUE_OAUTH_CODE_MARKER" not in joined
    assert marker not in joined
    for record in caplog.records:
        qs = getattr(record, "query_string", "")
        if qs:
            assert "UNIQUE_OAUTH_CODE_MARKER" not in qs
            assert marker not in qs
            assert "REDACTED" in qs
