"""Bind publication-attempt tests to an isolated application database."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from revenue_os.models.publication_attempt import PublicationAttempt


def bind_attempt_ledger(monkeypatch: Any, tmp_path: Path, publishing_engine: Any) -> Path:
    """Point the attempt store at a temp SQLite file that stands in for the app DB."""
    path = tmp_path / "attempt_authority.db"
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    PublicationAttempt.__table__.create(bind=engine, checkfirst=True)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(publishing_engine, "SessionLocal", factory)
    monkeypatch.setattr(publishing_engine, "_attempt_table_ready", True)
    return path
