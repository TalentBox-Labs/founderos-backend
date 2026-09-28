"""Clean-build regression: bare PostgreSQL URLs must use declared psycopg2."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine

from revenue_os.db_url import lock_declared_postgres_driver, validate_database_url

_ROOT = Path(__file__).resolve().parent.parent


def test_requirements_pin_sqlalchemy_below_2_1() -> None:
    text = (_ROOT / "requirements-revenue.txt").read_text(encoding="utf-8")
    assert "sqlalchemy>=2.0.25,<2.1" in text
    assert "psycopg2-binary>=" in text


def test_bare_postgres_url_locks_to_psycopg2() -> None:
    bare = "postgresql://user:pass@db.example/neondb?sslmode=require"
    locked = validate_database_url(bare)
    assert locked == "postgresql+psycopg2://user:pass@db.example/neondb?sslmode=require"
    legacy = lock_declared_postgres_driver(
        "postgres://user:pass@db.example/neondb?sslmode=require"
    )
    assert legacy.startswith("postgresql+psycopg2://")


def test_explicit_psycopg_scheme_is_not_rewritten() -> None:
    explicit = "postgresql+psycopg://user:pass@db.example/neondb?sslmode=require"
    assert validate_database_url(explicit) == explicit


def test_locked_url_loads_psycopg2_dialect_without_connecting() -> None:
    url = validate_database_url("postgresql://user:pass@127.0.0.1/db?sslmode=require")
    engine = create_engine(url)
    try:
        assert "psycopg2" in engine.dialect.__class__.__module__
        assert engine.dialect.driver == "psycopg2"
    finally:
        engine.dispose()
