"""Publication-ledger compatibility gate.

The application database owns a singleton compatibility row. This binary
may use PostgreSQL publication attempts only when that row matches
``LEDGER_GENERATION``. A missing row is sealed by this binary. A mismatched
or unreadable row fails closed. This module never opens
``publication_attempts.sqlite``.
"""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Any, Never

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError

from revenue_os.models.publication_attempt import PublicationAttempt
from revenue_os.models.publication_ledger_compatibility import (
    PublicationLedgerCompatibility,
)

LEDGER_GENERATION = 2
LEDGER_AUTHORITY = "postgresql"
INERT_CHECK_NAME = "ck_publication_attempts_inert_remote"
JOB_ID_UNIQUE_NAME = "uq_publication_attempts_job_id"
IDENTITY_COLUMNS = (
    "tenant_id",
    "content_id",
    "content_version",
    "channel",
    "destination",
)
ATTEMPT_CLASS_PUBLICATION = "publication"
ATTEMPT_CLASS_INERT_SENTINEL = "inert_sentinel"
SENTINEL_CONTENT_ID = "INERT-SENTINEL"
SENTINEL_CONTENT_VERSION = "inert-sentinel:v1"
SENTINEL_CHANNEL = "inert"
SENTINEL_DESTINATION = "inert:non-publishable"
SENTINEL_TRUTH = "inert_non_publishable"
SENTINEL_STATE = "inert_sentinel"
_migrate_lock = threading.Lock()
_ready_binds: set[str] = set()

_INERT_CHECK_SQL = (
    "ALTER TABLE publication_attempts ADD CONSTRAINT "
    "ck_publication_attempts_inert_remote CHECK ("
    "(attempt_class <> 'inert_sentinel') OR "
    "((NOT remote_write_outstanding) AND publication_truth = 'inert_non_publishable')"
    ")"
)


class LedgerState(Enum):
    COMPATIBLE = "LEDGER_COMPATIBLE"
    INCOMPATIBLE = "LEDGER_INCOMPATIBLE"
    UNKNOWN = "LEDGER_STATE_UNKNOWN"


class PublicationLedgerDenied(RuntimeError):
    """Publication authority cannot be proven. No SQLite fallback is attempted."""

    def __init__(self, state: LedgerState) -> None:
        self.state = state
        super().__init__(state.value)


def access_for(state: LedgerState) -> str:
    """ALLOW only for a compatible ledger. Every other state is DENY."""
    if state is LedgerState.COMPATIBLE:
        return "ALLOW"
    if state is LedgerState.INCOMPATIBLE:
        return "DENY"
    if state is LedgerState.UNKNOWN:
        return "DENY"
    unreachable: Never = state
    raise AssertionError(unreachable)


def decide_publication_access(
    *,
    reader_generation: int | None,
    stored_generation: int | None,
    stored_authority: str | None,
) -> LedgerState:
    """Map observed marker values to a ledger state. This does not write."""
    if reader_generation is None or stored_generation is None or not stored_authority:
        return LedgerState.UNKNOWN
    if reader_generation != stored_generation or stored_authority != LEDGER_AUTHORITY:
        return LedgerState.INCOMPATIBLE
    return LedgerState.COMPATIBLE


def admit_publication_process(
    *,
    binary_understands_guard: bool,
    state: LedgerState,
) -> str:
    """Platform admission. A binary that cannot read the guard is denied."""
    if not binary_understands_guard:
        return "DENY"
    return access_for(state)


def _session_factory_bind(session_factory: Any) -> Any:
    bind = getattr(session_factory, "kw", {}).get("bind")
    if bind is not None:
        return bind
    db = session_factory()
    try:
        return db.get_bind()
    finally:
        db.close()


def _default_session_factory() -> Any:
    from revenue_os.database import SessionLocal

    return SessionLocal


def migrate_publication_attempts(session_factory: Any) -> None:
    """Add the sentinel classification to a table created by an older binary.

    The lock serializes one-time DDL. It is not the publication uniqueness
    mechanism. Uniqueness remains the database primary key.
    """
    bind = _session_factory_bind(session_factory)
    key = bind.url.render_as_string(hide_password=True)
    if key in _ready_binds:
        return
    with _migrate_lock:
        if key in _ready_binds:
            return
        PublicationAttempt.__table__.create(bind=bind, checkfirst=True)
        PublicationLedgerCompatibility.__table__.create(bind=bind, checkfirst=True)
        inspector = inspect(bind)
        columns = {col["name"] for col in inspector.get_columns("publication_attempts")}
        dialect = bind.dialect.name
        with bind.begin() as conn:
            if "attempt_class" not in columns:
                conn.execute(
                    text(
                        "ALTER TABLE publication_attempts ADD COLUMN attempt_class "
                        "VARCHAR(40) DEFAULT 'publication' NOT NULL"
                    )
                )
            if dialect == "postgresql" and not _postgres_has_constraint(conn, INERT_CHECK_NAME):
                conn.execute(text(_INERT_CHECK_SQL))
        _ready_binds.add(key)


def _postgres_has_constraint(conn: Any, name: str) -> bool:
    row = conn.execute(
        text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
        {"name": name},
    ).first()
    return row is not None


def bootstrap_publication_ledger(session_factory: Any | None = None) -> LedgerState:
    """Seal generation 2 when absent, and never downgrade an existing marker."""
    factory = session_factory or _default_session_factory()
    for _attempt in range(6):
        try:
            return _bootstrap_once(factory)
        except OperationalError as exc:
            if "locked" not in str(exc).lower():
                return LedgerState.UNKNOWN
            time.sleep(0.05)
        except Exception:
            return LedgerState.UNKNOWN
    return LedgerState.UNKNOWN


def _bootstrap_once(factory: Any) -> LedgerState:
    migrate_publication_attempts(factory)
    db = factory()
    try:
        row = db.get(PublicationLedgerCompatibility, 1)
        if row is None:
            db.add(
                PublicationLedgerCompatibility(
                    singleton_id=1,
                    ledger_generation=LEDGER_GENERATION,
                    authority=LEDGER_AUTHORITY,
                )
            )
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
            row = db.get(PublicationLedgerCompatibility, 1)
        if row is None:
            return LedgerState.UNKNOWN
        return decide_publication_access(
            reader_generation=LEDGER_GENERATION,
            stored_generation=int(row.ledger_generation),
            stored_authority=str(row.authority or ""),
        )
    finally:
        db.close()


def require_publication_ledger(session_factory: Any | None = None) -> LedgerState:
    """Fail closed before any publication write. Does not open SQLite."""
    state = bootstrap_publication_ledger(session_factory)
    if access_for(state) != "ALLOW":
        raise PublicationLedgerDenied(state)
    return state


def inspect_publication_ledger_contract(session_factory: Any | None = None) -> dict[str, Any]:
    """Read constraint names from the live catalog. No connection string."""
    factory = session_factory or _default_session_factory()
    state = bootstrap_publication_ledger(factory)
    bind = _session_factory_bind(factory)
    inspector = inspect(bind)
    present = bool(inspector.has_table("publication_attempts"))
    identity: list[str] = []
    job_unique = ""
    inert_check = ""
    if present:
        primary = inspector.get_pk_constraint("publication_attempts")
        identity = [str(name) for name in (primary.get("constrained_columns") or [])]
        for unique in inspector.get_unique_constraints("publication_attempts"):
            columns = [str(name) for name in (unique.get("column_names") or [])]
            if columns == ["job_id"]:
                job_unique = str(unique.get("name") or "")
        for check in inspector.get_check_constraints("publication_attempts"):
            if str(check.get("name") or "") == INERT_CHECK_NAME:
                inert_check = INERT_CHECK_NAME
        if bind.dialect.name == "postgresql" and not inert_check:
            db = factory()
            try:
                if _postgres_has_constraint(db, INERT_CHECK_NAME):
                    inert_check = INERT_CHECK_NAME
            finally:
                db.close()
    return {
        "ledger_state": state.value,
        "publication_access": access_for(state),
        "table_present": present,
        "authority": LEDGER_AUTHORITY if state is LedgerState.COMPATIBLE else "",
        "generation": LEDGER_GENERATION if state is LedgerState.COMPATIBLE else None,
        "identity_columns": identity,
        "job_id_unique_constraint": job_unique,
        "inert_check_constraint": inert_check,
        "sqlite_authority": False,
        "catalog_source": "database_inspector",
    }


def main() -> int:
    """Deploy admission. Prints only the state token and ALLOW or DENY."""
    try:
        state = bootstrap_publication_ledger()
    except Exception:
        print(LedgerState.UNKNOWN.value)
        print("DENY")
        return 1
    print(state.value)
    decision = access_for(state)
    print(decision)
    return 0 if decision == "ALLOW" else 1


if __name__ == "__main__":
    raise SystemExit(main())
