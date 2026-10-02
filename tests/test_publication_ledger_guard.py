"""Rollback admission and inert publication-sentinel proof."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from revenue_os.models.publication_attempt import PublicationAttempt
from revenue_os.models.publication_ledger_compatibility import (
    PublicationLedgerCompatibility,
)
from src.tools import publishing_engine as pe
from src.tools.publication_ledger_guard import (
    IDENTITY_COLUMNS,
    INERT_CHECK_NAME,
    JOB_ID_UNIQUE_NAME,
    LEDGER_AUTHORITY,
    LEDGER_GENERATION,
    SENTINEL_TRUTH,
    LedgerState,
    PublicationLedgerDenied,
    access_for,
    admit_publication_process,
    bootstrap_publication_ledger,
    decide_publication_access,
    inspect_publication_ledger_contract,
)
from tests.attempt_ledger import bind_attempt_ledger


@pytest.fixture
def ledger_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    publishing = tmp_path / "output" / "publishing"
    publishing.mkdir(parents=True)
    monkeypatch.setattr(pe, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(pe, "PUBLISHING_DIR", publishing)
    monkeypatch.setattr(pe, "JOBS_JSONL", publishing / "jobs.jsonl")
    monkeypatch.setattr(pe, "AUDIT_JSONL", publishing / "audit.jsonl")
    monkeypatch.setattr(pe, "JOBS_DIR", publishing / "jobs")
    bind_attempt_ledger(monkeypatch, tmp_path, pe)
    return publishing


def _sentinel() -> dict[str, Any]:
    return pe.create_inert_sentinel_attempt(
        requested_by="Human A",
        tenant_id="tenant-a",
    )


def _attempt_count(publishing: Path) -> int:
    assert not (publishing / pe.ATTEMPTS_DB_NAME).exists()
    db = pe.SessionLocal()
    try:
        return int(db.query(PublicationAttempt).count())
    finally:
        db.close()


def test_access_matrix_fails_closed() -> None:
    assert access_for(LedgerState.COMPATIBLE) == "ALLOW"
    assert access_for(LedgerState.INCOMPATIBLE) == "DENY"
    assert access_for(LedgerState.UNKNOWN) == "DENY"
    assert (
        decide_publication_access(
            reader_generation=LEDGER_GENERATION,
            stored_generation=LEDGER_GENERATION,
            stored_authority=LEDGER_AUTHORITY,
        )
        is LedgerState.COMPATIBLE
    )
    assert (
        decide_publication_access(
            reader_generation=1,
            stored_generation=LEDGER_GENERATION,
            stored_authority=LEDGER_AUTHORITY,
        )
        is LedgerState.INCOMPATIBLE
    )
    assert (
        decide_publication_access(
            reader_generation=None,
            stored_generation=LEDGER_GENERATION,
            stored_authority=LEDGER_AUTHORITY,
        )
        is LedgerState.UNKNOWN
    )


def test_historical_binary_is_denied_and_does_not_create_sqlite(tmp_path: Path) -> None:
    created = {"called": False}

    def historical_sqlite_writer(directory: Path) -> None:
        created["called"] = True
        connection = sqlite3.connect(directory / "publication_attempts.sqlite")
        connection.execute("CREATE TABLE publication_attempts (job_id TEXT)")
        connection.execute("INSERT INTO publication_attempts (job_id) VALUES ('old')")
        connection.commit()
        connection.close()

    decision = admit_publication_process(
        binary_understands_guard=False,
        state=LedgerState.COMPATIBLE,
    )
    assert decision == "DENY"
    if decision == "ALLOW":
        historical_sqlite_writer(tmp_path)
    assert created["called"] is False
    assert not (tmp_path / "publication_attempts.sqlite").exists()


def test_incompatible_marker_denies_publication_without_sqlite(ledger_env: Path) -> None:
    assert bootstrap_publication_ledger(pe.SessionLocal) is LedgerState.COMPATIBLE
    db = pe.SessionLocal()
    try:
        row = db.get(PublicationLedgerCompatibility, 1)
        assert row is not None
        row.ledger_generation = 1
        db.commit()
    finally:
        db.close()
    with pytest.raises(PublicationLedgerDenied) as caught:
        pe.create_publish_job(
            content_id="W99",
            channel="website",
            requested_by="Human A",
            tenant_id="tenant-a",
        )
    assert caught.value.state is LedgerState.INCOMPATIBLE
    assert _attempt_count(ledger_env) == 0


def test_unknown_ledger_denies_without_sqlite(
    ledger_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_factory() -> Any:
        raise RuntimeError("catalog unreadable")

    monkeypatch.setattr(pe, "SessionLocal", broken_factory)
    with pytest.raises(PublicationLedgerDenied) as caught:
        _sentinel()
    assert caught.value.state is LedgerState.UNKNOWN
    assert not (ledger_env / pe.ATTEMPTS_DB_NAME).exists()


def test_sentinel_is_unique_inert_and_replayed(ledger_env: Path) -> None:
    calls = {"n": 0}

    def _adapter(job: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        return {"ok": True}

    pe.ADAPTERS["inert"] = _adapter
    try:
        first = _sentinel()
        second = _sentinel()
        assert first["job_id"] == second["job_id"]
        assert second["idempotent"] is True
        assert first["attempt_class"] == "inert_sentinel"
        assert first["remote_publishable"] is False
        assert first["publication_truth"] == SENTINEL_TRUTH
        assert first["adapter_call_count"] == 0
        assert _attempt_count(ledger_env) == 1
        with pytest.raises(ValueError, match="sentinel"):
            pe.manual_publish(first["job_id"], requested_by="Human A")
        with pytest.raises(ValueError, match="sentinel"):
            pe.retry_job(first["job_id"], requested_by="Human A")
        assert calls["n"] == 0
        assert not (ledger_env / "jobs").exists() or not list((ledger_env / "jobs").glob("*.json"))
        contract = inspect_publication_ledger_contract(pe.SessionLocal)
        assert contract["table_present"] is True
        assert contract["identity_columns"] == list(IDENTITY_COLUMNS)
        assert contract["job_id_unique_constraint"] == JOB_ID_UNIQUE_NAME
        assert contract["inert_check_constraint"] == INERT_CHECK_NAME
        assert contract["sqlite_authority"] is False
    finally:
        pe.ADAPTERS.pop("inert", None)


def test_sentinel_check_constraint_rejects_remote_write(ledger_env: Path) -> None:
    _sentinel()
    db = pe.SessionLocal()
    try:
        row = db.query(PublicationAttempt).one()
        row.remote_write_outstanding = True
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()
    assert _attempt_count(ledger_env) == 1


def test_sentinel_survives_a_new_session(ledger_env: Path) -> None:
    first = _sentinel()
    db = pe.SessionLocal()
    db.close()
    second = _sentinel()
    assert second["job_id"] == first["job_id"]
    assert second["idempotent"] is True
    assert _attempt_count(ledger_env) == 1


def test_concurrent_sentinel_creates_one_row(ledger_env: Path) -> None:
    barrier = threading.Barrier(4)
    results: list[dict[str, Any]] = []

    def _create() -> None:
        barrier.wait()
        results.append(_sentinel())

    threads = [threading.Thread(target=_create) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 4
    assert len({item["job_id"] for item in results}) == 1
    assert _attempt_count(ledger_env) == 1


def test_sentinel_api_is_hidden_from_the_publication_queue(
    cms_client: TestClient, ledger_env: Path
) -> None:
    created = cms_client.post(
        "/api/v1/publishing/sentinel",
        json={"requested_by": "API Human", "organization_id": "client-tenant"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["job"]["tenant_id"] == "test-key"
    assert body["job"]["remote_publishable"] is False
    assert body["job"]["attempt_class"] == "inert_sentinel"
    replay = cms_client.post(
        "/api/v1/publishing/sentinel",
        json={"requested_by": "API Human"},
    )
    assert replay.status_code == 200
    assert replay.json()["job_id"] == body["job_id"]
    assert replay.json()["job"]["idempotent"] is True
    fetched = cms_client.get("/api/v1/publishing/sentinel")
    assert fetched.status_code == 200
    assert fetched.json()["job_id"] == body["job_id"]
    queued = cms_client.get("/api/v1/publishing/jobs")
    assert queued.status_code == 200
    assert queued.json()["count"] == 0
    rejected = cms_client.post(
        "/api/v1/publishing/jobs",
        json={
            "content_id": "INERT-SENTINEL",
            "channel": "website",
            "requested_by": "API Human",
        },
    )
    assert rejected.status_code == 400
    assert _attempt_count(ledger_env) == 1


def test_image_start_runs_the_guard_before_the_server() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["sh", "scripts/publication_admission_start.sh"]' in dockerfile
    script = Path("scripts/publication_admission_start.sh").read_text(encoding="utf-8")
    assert script.index("publication_ledger_guard") < script.index("exec uvicorn")


def test_admission_start_does_not_exec_the_server_when_the_guard_denies(
    tmp_path: Path,
) -> None:
    _assert_admission_start(tmp_path, guard_status=1, server_runs=False)


def test_admission_start_execs_the_server_when_the_guard_allows(tmp_path: Path) -> None:
    _assert_admission_start(tmp_path, guard_status=0, server_runs=True)


def test_tree_without_the_start_script_cannot_become_the_process(tmp_path: Path) -> None:
    completed = subprocess.run(
        ["sh", "scripts/publication_admission_start.sh"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0


def _assert_admission_start(tmp_path: Path, *, guard_status: int, server_runs: bool) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "server-started"
    python = bin_dir / "python"
    uvicorn = bin_dir / "uvicorn"
    python.write_text(f"#!/bin/sh\nexit {guard_status}\n", encoding="utf-8")
    uvicorn.write_text(
        f"#!/bin/sh\ntouch {marker}\nexit 0\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    uvicorn.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    completed = subprocess.run(
        ["sh", "scripts/publication_admission_start.sh"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        check=False,
    )
    assert marker.exists() is server_runs
    if server_runs:
        assert completed.returncode == 0
    else:
        assert completed.returncode != 0


def test_health_ledger_reports_the_catalog(cms_client: TestClient) -> None:
    response = cms_client.get("/health/ledger")
    assert response.status_code == 200
    body = response.json()
    assert body["ledger_state"] == LedgerState.COMPATIBLE.value
    assert body["publication_access"] == "ALLOW"
    assert body["table_present"] is True
    assert body["identity_columns"] == list(IDENTITY_COLUMNS)
    assert body["job_id_unique_constraint"] == JOB_ID_UNIQUE_NAME
    assert body["inert_check_constraint"] == INERT_CHECK_NAME
    assert body["sqlite_authority"] is False
    assert "postgresql://" not in response.text
    assert "postgres://" not in response.text
