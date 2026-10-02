"""Canonical publication-attempt identity and atomic uniqueness."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from revenue_os.models.publication_attempt import PublicationAttempt
from src.tools import editorial_approval as ea
from tests.attempt_ledger import bind_attempt_ledger
from src.tools import publishing_engine as pe
from src.tools.content_ops_readiness import autonomous_publication_ready
from src.ui.content_ops_beta.presenter import present_content_ops_read


def _seed_approval(decisions: Path, content_id: str = "W99") -> None:
    decisions.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
        "decision_id": "dec-test-1",
        "utc_timestamp": "2026-08-09T12:00:00Z",
        "content_id": content_id,
        "phase": "3",
        "bundle": f"output/generated/{content_id}",
        "decision": "approve",
        "approver": "Editor Human",
        "notes": "",
        "promotion": {"attempted": True, "ok": True, "exit_code": 0},
        "authorizes_publish": False,
    }
    with (decisions / "decisions.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


@pytest.fixture
def publishing_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    decisions = tmp_path / "output" / "editorial_decisions"
    publishing = tmp_path / "output" / "publishing"
    publishing.mkdir(parents=True)
    _seed_approval(decisions)
    monkeypatch.setattr(ea, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ea, "DECISIONS_DIR", decisions)
    monkeypatch.setattr(ea, "HISTORY_JSONL", decisions / "decisions.jsonl")
    monkeypatch.setattr(pe, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(pe, "PUBLISHING_DIR", publishing)
    monkeypatch.setattr(pe, "JOBS_JSONL", publishing / "jobs.jsonl")
    monkeypatch.setattr(pe, "AUDIT_JSONL", publishing / "audit.jsonl")
    monkeypatch.setattr(pe, "JOBS_DIR", publishing / "jobs")
    bind_attempt_ledger(monkeypatch, tmp_path, pe)
    return {"tmp_path": tmp_path, "publishing": publishing}


def _job(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("tenant_id", "tenant-a")
    kwargs.setdefault("content_id", "W99")
    kwargs.setdefault("channel", "website")
    kwargs.setdefault("requested_by", "Human A")
    return pe.create_publish_job(**kwargs)


def _attempt_count(publishing: Path) -> int:
    assert not (publishing / pe.ATTEMPTS_DB_NAME).exists()
    db = pe.SessionLocal()
    try:
        return int(db.query(PublicationAttempt).count())
    finally:
        db.close()


def test_same_identity_replays_one_job(publishing_env: dict[str, Any]) -> None:
    first = _job()
    second = _job(notes="again")
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["job_id"] == first["job_id"]
    assert second["publication_truth"] == "unproven"
    assert second["state"] == pe.STATE_PUBLISH_PENDING
    assert _attempt_count(publishing_env["publishing"]) == 1
    assert len(list((publishing_env["publishing"] / "jobs").glob("*.json"))) == 1


def test_distinct_tenants_are_distinct_attempts(publishing_env: dict[str, Any]) -> None:
    first = _job(tenant_id="tenant-a")
    second = _job(tenant_id="tenant-b")
    assert first["job_id"] != second["job_id"]
    assert _attempt_count(publishing_env["publishing"]) == 2


def test_new_artifact_version_is_a_new_attempt(publishing_env: dict[str, Any]) -> None:
    first = _job()
    assert first["content_version"].startswith("editorial-decision:")
    final = publishing_env["tmp_path"] / "input" / "W99" / "05_Final.md"
    final.parent.mkdir(parents=True)
    final.write_text("version-one\n", encoding="utf-8")
    revised = _job()
    assert revised["job_id"] != first["job_id"]
    assert revised["content_version"].startswith("sha256:")
    final.write_text("version-one\n", encoding="utf-8")
    replay = _job()
    assert replay["job_id"] == revised["job_id"]
    assert replay["idempotent"] is True
    assert replay["publication_truth"] == "unproven"


def test_channel_changes_destination(publishing_env: dict[str, Any]) -> None:
    website = _job(channel="website")
    linkedin = _job(channel="linkedin")
    assert website["destination"] == "website:default"
    assert linkedin["destination"] == "linkedin:default"
    assert website["job_id"] != linkedin["job_id"]


def test_simultaneous_creates_one_attempt(publishing_env: dict[str, Any]) -> None:
    barrier = threading.Barrier(4)
    found: list[str] = []
    errors: list[BaseException] = []

    def _create() -> None:
        try:
            barrier.wait()
            job = _job()
            found.append(str(job["job_id"]))
        except BaseException as exc:  # noqa: BLE001 — collect worker failures
            errors.append(exc)

    workers = [threading.Thread(target=_create) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert errors == []
    assert len(found) == 4
    assert len(set(found)) == 1
    assert _attempt_count(publishing_env["publishing"]) == 1


def test_retry_after_remote_confirmation_does_not_open_another_write(
    publishing_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def _remote(job: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        return {
            "ok": True,
            "remote_write_acknowledged": True,
            "remote_object_id": "cms-object-1",
            "external_http": True,
            "rendering_performed": True,
        }

    monkeypatch.setitem(pe.ADAPTERS, "website", _remote)
    created = _job()
    published = pe.manual_publish(created["job_id"], requested_by="Human A")
    replay = _job()
    assert calls["n"] == 1
    assert replay["job_id"] == created["job_id"]
    assert replay["publication_truth"] == published["publication_truth"]
    assert replay["verification_status"] == "verification_pending"
    assert replay["state"] == pe.STATE_PUBLISHED
    with pytest.raises(LookupError, match="already published"):
        pe.manual_publish(created["job_id"], requested_by="Human A")
    assert calls["n"] == 1


def test_placeholder_success_is_not_published(publishing_env: dict[str, Any]) -> None:
    created = _job()
    result = pe.manual_publish(created["job_id"], requested_by="Human A")
    assert result["state"] != pe.STATE_PUBLISHED
    assert result["publication_truth"] == "unproven"
    assert result["verification_status"] != "verified"


def test_editorial_approval_does_not_authorize_publish(
    publishing_env: dict[str, Any],
) -> None:
    source = Path(ea.__file__).read_text(encoding="utf-8")
    decision_fn = source.split("def apply_decision(", 1)[1].split("\ndef build_item_view(", 1)[0]
    assert "create_publish_job" not in decision_fn
    assert "manual_publish" not in decision_fn
    outcome = ea.apply_decision(
        content_id="W98",
        decision="request_changes",
        approver="Editor Human",
        phase="3",
        notes="hold",
    )
    assert outcome["record"]["authorizes_publish"] is False
    assert _attempt_count(publishing_env["publishing"]) == 0


def test_database_primary_key_rejects_a_second_insert(
    publishing_env: dict[str, Any],
) -> None:
    _job()
    db = pe.SessionLocal()
    try:
        row = db.query(PublicationAttempt).one()
        db.expunge(row)
        db.add(
            PublicationAttempt(
                tenant_id=row.tenant_id,
                content_id=row.content_id,
                content_version=row.content_version,
                channel=row.channel,
                destination=row.destination,
                job_id="pub_other",
                created_at=row.created_at,
                requested_by=row.requested_by,
                state=row.state,
                publication_truth=row.publication_truth,
                verification_status=row.verification_status,
                remote_write_outstanding=False,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()
    assert _attempt_count(publishing_env["publishing"]) == 1


def test_attempt_survives_snapshot_loss(publishing_env: dict[str, Any]) -> None:
    first = _job()
    for path in (publishing_env["publishing"] / "jobs").glob("*.json"):
        path.unlink()
    replay = _job()
    assert replay["job_id"] == first["job_id"]
    assert replay["idempotent"] is True
    assert replay["state"] == pe.STATE_PUBLISH_PENDING
    assert _attempt_count(publishing_env["publishing"]) == 1


def test_dropped_remote_call_cannot_retry_after_snapshot_loss(
    publishing_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def _remote(job: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        raise RuntimeError("response lost")

    monkeypatch.setitem(pe.ADAPTERS, "website", _remote)
    created = _job()
    with pytest.raises(RuntimeError, match="response lost"):
        pe.manual_publish(created["job_id"], requested_by="Human A")
    for path in (publishing_env["publishing"] / "jobs").glob("*.json"):
        path.unlink()
    with pytest.raises(ValueError, match="unknown"):
        pe.manual_publish(created["job_id"], requested_by="Human A")
    assert calls["n"] == 1
    assert _attempt_count(publishing_env["publishing"]) == 1


def test_unknown_remote_survives_snapshot_loss(
    publishing_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def _remote(job: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        return {
            "ok": False,
            "remote_request_dispatched": True,
            "remote_outcome": "timeout",
        }

    monkeypatch.setitem(pe.ADAPTERS, "website", _remote)
    created = _job()
    pe.manual_publish(created["job_id"], requested_by="Human A")
    for path in (publishing_env["publishing"] / "jobs").glob("*.json"):
        path.unlink()
    with pytest.raises(ValueError, match="unknown"):
        pe.retry_job(created["job_id"], requested_by="Human A")
    assert calls["n"] == 1
    assert _attempt_count(publishing_env["publishing"]) == 1


def test_api_ignores_client_organization(
    cms_client: TestClient, publishing_env: dict[str, Any]
) -> None:
    first = cms_client.post(
        "/api/v1/publishing/jobs",
        json={
            "content_id": "W99",
            "channel": "website",
            "requested_by": "API Human",
            "organization_id": "client-tenant",
            "tenant_id": "client-tenant",
        },
    )
    assert first.status_code == 200
    second = cms_client.post(
        "/api/v1/publishing/jobs",
        json={
            "content_id": "W99",
            "channel": "website",
            "requested_by": "Other Human",
            "organization_id": "another-tenant",
        },
    )
    assert second.status_code == 200
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["job"]["tenant_id"] == "test-key"
    assert second.json()["job"]["idempotent"] is True
    assert _attempt_count(publishing_env["publishing"]) == 1


def test_autonomous_publication_stays_closed() -> None:
    assert autonomous_publication_ready() is False
    view = present_content_ops_read(
        {
            "ok": True,
            "live": True,
            "source": "content_ops_publication_truth",
            "state": "ready",
            "projection": {
                "week_id": "W01",
                "stage": "human_review",
                "qa_status": "PASS",
                "publication_status": "not_started",
                "publication_truth": "unproven",
                "verification_status": "unproven",
                "allowed_actions": ["view_audit"],
            },
        }
    )
    assert view["readiness_surface"] == "content_review_internal_beta"
    assert view["autonomous_publication_ready"] is False
