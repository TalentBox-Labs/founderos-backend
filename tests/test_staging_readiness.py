"""Staging readiness: fail-closed config, TLS URL rules, idle switches, health semantics."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from revenue_os.db_url import (
    SECRET_KEY_MIN_LENGTH,
    assert_pool_within_limit,
    assert_postgres_tls_unambiguous,
    bootstrap_password_acceptable,
    max_app_pool_connections,
    postgres_tls_configured,
    redact_database_url,
    sanitize_exception_for_log,
    text_contains_credential_material,
    validate_database_url,
    validate_secret_key,
)
from revenue_os.scheduler import heartbeat_enabled, initialize_heartbeat, scheduler


def test_secret_key_missing_and_placeholders_fail_closed() -> None:
    with pytest.raises(ValueError, match="SECRET_KEY"):
        validate_secret_key(None)
    with pytest.raises(ValueError, match="SECRET_KEY"):
        validate_secret_key("")
    with pytest.raises(ValueError, match="whitespace"):
        validate_secret_key(" ")
    with pytest.raises(ValueError, match="whitespace"):
        validate_secret_key("       ")
    with pytest.raises(ValueError, match="leading or trailing"):
        validate_secret_key("  good-enough-secret-key-with-padding-xx  ")
    with pytest.raises(ValueError, match="at least"):
        validate_secret_key("a")
    with pytest.raises(ValueError, match="at least"):
        validate_secret_key("abc")
    with pytest.raises(ValueError, match="at least"):
        validate_secret_key("x" * (SECRET_KEY_MIN_LENGTH - 1))
    with pytest.raises(ValueError, match="forbidden"):
        validate_secret_key("change-me-in-production")
    with pytest.raises(ValueError, match="repeated"):
        validate_secret_key("x" * SECRET_KEY_MIN_LENGTH)


def test_secret_key_minimum_and_strong_test_value_accepted() -> None:
    # Exactly 32 chars, not a repeated single character (also forbidden).
    at_min = ("abcdefghij" * 3) + "kl"
    assert len(at_min) == SECRET_KEY_MIN_LENGTH
    assert validate_secret_key(at_min) == at_min
    strong = "test-secret-key-not-for-production-use-only-in-ci"
    assert len(strong) >= SECRET_KEY_MIN_LENGTH
    assert validate_secret_key(strong) == strong


def test_database_url_missing_fails_closed() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL must be set"):
        validate_database_url("")
    with pytest.raises(ValueError, match="DATABASE_URL must be set"):
        validate_database_url(None)


def test_postgres_tls_secure_modes_accepted() -> None:
    for mode in ("require", "verify-ca", "verify-full"):
        url = f"postgresql://user:pass@ep-example.us-east-2.aws.neon.tech/neondb?sslmode={mode}"
        assert validate_database_url(url) == url
        assert postgres_tls_configured(url) is True
    ssl_true = "postgresql://user:pass@ep-example.us-east-2.aws.neon.tech/neondb?ssl=true"
    assert validate_database_url(ssl_true) == ssl_true


def test_postgres_insecure_and_missing_tls_rejected() -> None:
    base = "postgresql://user:pass@ep-example.neon.tech/neondb"
    with pytest.raises(ValueError, match="TLS|sslmode"):
        validate_database_url(base)
    for mode in ("disable", "allow", "prefer"):
        with pytest.raises(ValueError, match="sslmode|TLS|secure|not permitted"):
            validate_database_url(f"{base}?sslmode={mode}")


def test_postgres_tls_ambiguity_fails_closed() -> None:
    base = "postgresql://user:pass@ep-example.neon.tech/neondb"
    # Duplicate identical or contradictory sslmode — always reject.
    for q in (
        "sslmode=require&sslmode=require",
        "sslmode=require&sslmode=disable",
        "sslmode=disable&sslmode=require",
        "sslmode=verify-full&sslmode=disable",
        "ssl=true&ssl=true",
        "sslmode=require&ssl=false",
        "sslmode=disable&ssl=true",
    ):
        with pytest.raises(
            ValueError,
            match="ambiguous|contradict|not once|not permitted|sslmode|TLS|secure",
        ):
            validate_database_url(f"{base}?{q}")
            assert_postgres_tls_unambiguous(f"{base}?{q}")


def test_sqlite_allowed_for_tests() -> None:
    assert validate_database_url("sqlite:////tmp/x.db").startswith("sqlite:")


def test_pool_hard_ceiling_is_30() -> None:
    assert max_app_pool_connections(10, 20) == 30
    assert_pool_within_limit(10, 20, limit=30)
    with pytest.raises(ValueError, match="exceeds limit 30"):
        assert_pool_within_limit(20, 20, limit=30)


def test_redact_database_url_strips_password() -> None:
    redacted = redact_database_url(
        "postgresql://alice:super-secret@host.example/db?sslmode=require"
    )
    assert "super-secret" not in redacted
    assert "alice" in redacted
    assert "***" in redacted


def test_sanitize_exception_never_includes_message() -> None:
    class FakeExc(Exception):
        pass

    exc = FakeExc(
        "could not connect to "
        "postgresql://SENTINEL_DB_USER:SENTINEL_DB_PASSWORD_DO_NOT_LOG@host/db"
    )
    sanitized = sanitize_exception_for_log(exc)
    assert sanitized == "FakeExc"
    assert "SENTINEL_DB_PASSWORD_DO_NOT_LOG" not in sanitized
    assert "postgresql://" not in sanitized


def test_heartbeat_disabled_does_not_start_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEARTBEAT_ENABLED", "0")
    assert heartbeat_enabled() is False
    with patch.object(scheduler, "start") as start:
        with patch.object(scheduler, "register"):
            initialize_heartbeat()
        start.assert_not_called()


def test_n8n_bridge_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("N8N_BRIDGE_ENABLED", "0")
    from revenue_os.integrations.n8n import initialize_n8n_bridge

    assert initialize_n8n_bridge() is False


def test_bootstrap_password_rejects_defaults() -> None:
    assert bootstrap_password_acceptable("password") is False
    assert bootstrap_password_acceptable("changeme") is False
    assert bootstrap_password_acceptable("short") is False
    assert bootstrap_password_acceptable("a-sufficiently-long-staging-password") is True


def test_empty_database_bootstrap_creates_session_revocations(tmp_path) -> None:
    """Local empty-DB bootstrap via create_all (SQLite stand-in for empty Postgres)."""
    db_path = tmp_path / "empty_bootstrap.db"
    url = f"sqlite:///{db_path}"
    from sqlalchemy import create_engine, inspect

    import revenue_os.models  # noqa: F401
    from revenue_os.models.base import Base

    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    tables = set(inspect(engine).get_table_names())
    assert "session_revocations" in tables
    assert "users" in tables
    assert "organizations" in tables
    assert "organization_memberships" in tables
    assert "connector_credentials" in tables
    engine.dispose()


def test_render_yaml_has_no_paid_starter_or_managed_db() -> None:
    from pathlib import Path

    text = Path("render.yaml").read_text(encoding="utf-8")
    assert "plan: starter" not in text
    assert "\ndatabases:" not in text and not text.lstrip().startswith("databases:")
    assert "plan: free" in text
    assert 'value: "0"' in text
    assert "HEARTBEAT_ENABLED" in text
    assert "N8N_BRIDGE_ENABLED" in text
    assert "autoDeploy: false" in text


@pytest.fixture
def client() -> TestClient:
    from runner_api import app

    return TestClient(app)


def test_health_ready_http_and_logs_omit_sentinel_credentials(
    client: TestClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel_pw = "SENTINEL_DB_PASSWORD_DO_NOT_LOG"
    sentinel_url = (
        f"postgresql://SENTINEL_DB_USER:{sentinel_pw}@db.example.invalid/neondb?sslmode=require"
    )

    class Boom(Exception):
        def __str__(self) -> str:  # noqa: D105
            return f"could not connect using {sentinel_url}"

    class FakeSession:
        def execute(self, *_a, **_k):  # noqa: ANN001
            raise Boom("connect failed")

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "revenue_os.database.SessionLocal",
        lambda: FakeSession(),
    )

    with caplog.at_level(logging.ERROR):
        r = client.get("/health/ready")

    assert r.status_code == 503
    body = r.text
    assert sentinel_pw not in body
    assert sentinel_url not in body
    assert "SENTINEL_DB_USER" not in body
    assert r.json()["database"] == "unavailable"

    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert not text_contains_credential_material(joined, sentinel_password=sentinel_pw)
    assert sentinel_pw not in joined
    assert sentinel_url not in joined


def test_startup_failure_logging_omits_sentinel_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from revenue_os.db_url import sanitize_exception_for_log

    sentinel_pw = "SENTINEL_DB_PASSWORD_DO_NOT_LOG"
    exc = RuntimeError(
        f"OperationalError: connection to "
        f"postgresql://SENTINEL_DB_USER:{sentinel_pw}@host/db failed"
    )
    with caplog.at_level(logging.ERROR):
        logging.getLogger("runner_api").error(
            "Table creation failed (fail-closed): type=%s",
            sanitize_exception_for_log(exc),
        )
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert sentinel_pw not in joined
    assert "postgresql://" not in joined
    assert "RuntimeError" in joined
