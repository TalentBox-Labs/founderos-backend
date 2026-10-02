"""Combined P0 convergence: publication truth, human beta tenant, current-week read."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from revenue_os.auth import hash_password
from revenue_os.models.organization import (
    MembershipStatus,
    Organization,
    OrganizationMembership,
    OrganizationStatus,
)
from revenue_os.models.user import User
from revenue_os.services.content_ops_authority import CONTENT_OPS_BETA_ORGANIZATION_ENV
import revenue_os.services.tenant_resolution as tenant_resolution_mod
import runner_api_routers.identity as identity_mod
from runner_api import app
from runner_api_routers.content_ops import router as content_ops_router
from src.tools import content_ops_current_week as current_week
from src.tools import editorial_approval as ea
from src.tools import publication_truth as truth
from src.tools import publishing_engine as pe
from tests.attempt_ledger import bind_attempt_ledger
from src.tools.publication_truth import (
    TRUTH_FAILED,
    TRUTH_REMOTE_WRITE_CONFIRMED,
    TRUTH_UNKNOWN_REMOTE,
    TRUTH_UNPROVEN,
    TRUTH_VERIFICATION_PENDING,
    TRUTH_VERIFIED,
)

pytestmark = pytest.mark.real_api_auth

_PASSWORD = "ContentOpsP0Authority1!"
_NAME = "Krishna Founder"
_SERVICE_KEY = "content-ops-p0-service-key"
_TRACKER_HEADER = (
    "content_id,title,status,qa_status,current_step,next_step,"
    "draft_path,qa_output_path,final_output_path,artifact_folder\n"
)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def identity_db(monkeypatch: pytest.MonkeyPatch) -> sessionmaker:
    from revenue_os.database import engine

    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(identity_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(tenant_resolution_mod, "SessionLocal", session_factory)
    return session_factory


def _user(db, *, name: str = _NAME) -> tuple[User, str]:
    email = f"p0-read-{uuid.uuid4().hex}@talentbox.invalid"
    user = User(
        email=email,
        hashed_password=hash_password(_PASSWORD),
        full_name=name,
        role="owner",
        is_active=1,
    )
    db.add(user)
    db.flush()
    return user, email


def _org(db, label: str) -> Organization:
    org = Organization(
        name=label,
        slug=f"{label}-{uuid.uuid4().hex[:8]}",
        status=OrganizationStatus.ACTIVE,
    )
    db.add(org)
    db.flush()
    return org


def _member(db, user: User, org: Organization) -> None:
    db.add(
        OrganizationMembership(
            user_id=user.id,
            organization_id=org.id,
            role="owner",
            status=MembershipStatus.ACTIVE,
        )
    )


def _login(client: TestClient, email: str) -> None:
    response = client.post(
        "/api/v1/identity/login",
        json={"email": email, "password": _PASSWORD},
    )
    assert response.status_code == 200, response.text


def _week_tree(
    tmp_path: Path,
    *,
    week: str = "W12",
    active: str | None = "W12",
    status: str = "published",
    extra_rows: str = "",
    draft: bool = True,
    final: bool = True,
) -> None:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    runtime: dict[str, Any] = {}
    if active is not None:
        runtime["active_week"] = active
    (tmp_path / "data" / "runtime_config.json").write_text(
        json.dumps(runtime), encoding="utf-8"
    )
    (tmp_path / "tracker.csv").write_text(
        _TRACKER_HEADER
        + f"{week},Title,{status},PASS,Publish Review,CMS Go-live,,,,,,\n"
        + extra_rows,
        encoding="utf-8",
    )
    folder = tmp_path / "input" / week
    folder.mkdir(parents=True, exist_ok=True)
    if draft:
        (folder / "04_Draft.md").write_text("draft\n", encoding="utf-8")
    if final:
        (folder / "05_Final.md").write_text(
            "---\nstatus: published\npublish_status: published\n"
            "canonical_url: https://example.invalid/legacy\n---\n",
            encoding="utf-8",
        )


def _write_job(tmp_path: Path, **fields: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "job_id": "pub_w12_website_test",
        "bundle_id": "W12",
        "state": "failed",
        "publication_truth": TRUTH_UNPROVEN,
        "verification_status": TRUTH_UNPROVEN,
        "remote_object_id": "",
        "remote_write_outstanding": False,
        "errors": [],
        "updated_at": "2026-09-29T00:00:00Z",
    }
    job.update(fields)
    folder = tmp_path / "output" / "publishing" / "jobs"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{job['job_id']}.json").write_text(
        json.dumps(job), encoding="utf-8"
    )
    return job


def _projection(tmp_path: Path) -> dict[str, Any]:
    payload = current_week.read_current_week(tmp_path)
    assert payload["http_status"] == 200, payload
    assert payload["ok"] is True
    projection = payload["projection"]
    assert isinstance(projection, dict)
    return projection


def _digest(path: Path) -> str:
    if not path.exists():
        return "missing"
    if path.is_dir():
        names = sorted(item.name for item in path.iterdir())
        return hashlib.sha256("\n".join(names).encode()).hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_route_is_get_only() -> None:
    methods = {
        method
        for route in content_ops_router.routes
        for method in getattr(route, "methods", set())
    }
    assert methods == {"GET"}
    assert "pause_week" not in current_week._ACTION_ORDER
    assert "resume" not in current_week._ACTION_ORDER


def test_canonical_week_ignores_runtime_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _week_tree(tmp_path, week="W12", active="W12")
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"active_week": "W02"}), encoding="utf-8")
    monkeypatch.setenv("WORKCREW_RUNTIME_CONFIG", str(other))
    projection = _projection(tmp_path)
    assert projection["week_id"] == "W12"


def test_no_current_week_is_empty(tmp_path: Path) -> None:
    _week_tree(tmp_path, active=None)
    payload = current_week.read_current_week(tmp_path)
    assert payload["http_status"] == 200
    assert payload["state"] == "empty"
    assert payload["reason"] == "no_current_week"
    assert payload["projection"]["week_id"] is None
    assert payload["projection"]["allowed_actions"] == []


def test_malformed_week_does_not_read_outside_root(tmp_path: Path) -> None:
    _week_tree(tmp_path, active="../W12")
    secret = tmp_path.parent / "secret-week.txt"
    secret.write_text("nope", encoding="utf-8")
    payload = current_week.read_current_week(tmp_path)
    assert payload["state"] == "empty"
    assert payload["reason"] == "malformed_current_week"
    assert payload["projection"]["week_id"] is None
    assert secret.read_text(encoding="utf-8") == "nope"


def test_missing_tracker_week_is_empty(tmp_path: Path) -> None:
    _week_tree(tmp_path, week="W12", active="W99")
    payload = current_week.read_current_week(tmp_path)
    assert payload["reason"] == "current_week_not_in_tracker"
    assert payload["projection"]["week_id"] is None


def test_duplicate_tracker_week_conflicts(tmp_path: Path) -> None:
    _week_tree(
        tmp_path,
        extra_rows="W12,Other,published,PASS,Completed,None,,,,,,\n",
    )
    payload = current_week.read_current_week(tmp_path)
    assert payload["http_status"] == 409
    assert payload["projection"] is None
    assert payload["error"]["code"] == "ACTIVE_INTENT"


def test_legacy_published_claim_stays_unproven(tmp_path: Path) -> None:
    _week_tree(tmp_path, status="published")
    _write_job(
        tmp_path,
        state="published",
        publication_truth=TRUTH_UNPROVEN,
        adapter_ok=True,
        http_status=200,
        rendering_performed=False,
        external_http=False,
        published_url="https://example.invalid/local",
    )
    projection = _projection(tmp_path)
    assert projection["publication_truth"] == TRUTH_UNPROVEN
    assert projection["verification_status"] == TRUTH_UNPROVEN
    assert projection["publication_status"] == "not_started"
    assert projection["published_url"] is None
    assert "open_published_url" not in projection["allowed_actions"]
    assert projection["stage"] != "verified"


def test_remote_write_confirmed_is_not_verified(tmp_path: Path) -> None:
    _week_tree(tmp_path)
    _write_job(
        tmp_path,
        state="published",
        publication_truth=TRUTH_REMOTE_WRITE_CONFIRMED,
        verification_status=TRUTH_VERIFICATION_PENDING,
        remote_object_id="cms-12",
        published_url="https://example.invalid/w12",
    )
    projection = _projection(tmp_path)
    assert projection["publication_truth"] == TRUTH_REMOTE_WRITE_CONFIRMED
    assert projection["verification_status"] == TRUTH_VERIFICATION_PENDING
    assert projection["published_url"] == "https://example.invalid/w12"
    assert projection["stage"] == "verification"
    assert "open_published_url" not in projection["allowed_actions"]


def test_verified_requires_matching_readback(tmp_path: Path) -> None:
    _week_tree(tmp_path)
    _write_job(
        tmp_path,
        state="published",
        publication_truth=TRUTH_VERIFIED,
        verification_status=TRUTH_VERIFIED,
        remote_object_id="cms-12",
        published_url="https://example.invalid/w12",
    )
    forged = _projection(tmp_path)
    assert forged["publication_truth"] == TRUTH_UNPROVEN
    assert "open_published_url" not in forged["allowed_actions"]

    _write_job(
        tmp_path,
        state="published",
        publication_truth=TRUTH_VERIFIED,
        verification_status=TRUTH_VERIFIED,
        remote_object_id="cms-12",
        published_url="https://example.invalid/w12",
        readback={
            "readback_performed": True,
            "remote_object_id": "cms-12",
            "content_hash": "abc",
            "readback_matches": True,
        },
    )
    verified = _projection(tmp_path)
    assert verified["publication_truth"] == TRUTH_VERIFIED
    assert verified["verification_status"] == TRUTH_VERIFIED
    assert "open_published_url" in verified["allowed_actions"]


def test_failed_and_unknown_remote(tmp_path: Path) -> None:
    _week_tree(tmp_path)
    _write_job(
        tmp_path,
        state="failed",
        publication_truth=TRUTH_FAILED,
        verification_status=TRUTH_FAILED,
        errors=["provider rejected the write"],
    )
    failed = _projection(tmp_path)
    assert failed["publication_truth"] == TRUTH_FAILED
    assert failed["failure"] == "provider rejected the write"
    assert "retry" in failed["allowed_actions"]

    _write_job(
        tmp_path,
        state="failed",
        publication_truth=TRUTH_UNKNOWN_REMOTE,
        verification_status=TRUTH_UNKNOWN_REMOTE,
        remote_write_outstanding=True,
        errors=["timeout"],
    )
    unknown = _projection(tmp_path)
    assert unknown["publication_truth"] == TRUTH_UNKNOWN_REMOTE
    assert unknown["risk_class"] == "unknown_remote"
    assert "retry" not in unknown["allowed_actions"]
    assert "cancel" not in unknown["allowed_actions"]
    assert "open_published_url" not in unknown["allowed_actions"]


def test_active_publish_conflicts(tmp_path: Path) -> None:
    _week_tree(tmp_path)
    _write_job(tmp_path, state="publishing", publication_truth=TRUTH_UNPROVEN)
    payload = current_week.read_current_week(tmp_path)
    assert payload["http_status"] == 409
    assert payload["ok"] is False


def test_allowed_actions_are_not_client_fields(tmp_path: Path) -> None:
    _week_tree(tmp_path, draft=True, final=False)
    (tmp_path / "input" / "W12" / "05_Final.md").unlink(missing_ok=True)
    projection = _projection(tmp_path)
    assert "pause_week" not in projection["allowed_actions"]
    assert "resume" not in projection["allowed_actions"]
    assert "run_now" in projection["allowed_actions"]
    assert projection["next_schedule_at"] is None


def test_read_does_not_mutate(tmp_path: Path) -> None:
    _week_tree(tmp_path)
    watched = [
        tmp_path / "tracker.csv",
        tmp_path / "data" / "runtime_config.json",
        tmp_path / "input" / "W12" / "05_Final.md",
        tmp_path / "output",
    ]
    before = {path: _digest(path) for path in watched}
    current_week.read_current_week(tmp_path)
    after = {path: _digest(path) for path in watched}
    assert before == after


def test_real_legacy_week_is_unproven() -> None:
    payload = current_week.read_current_week()
    assert payload["http_status"] == 200
    projection = payload["projection"]
    assert projection["week_id"] == "W01"
    assert projection["publication_truth"] == TRUTH_UNPROVEN
    assert projection["verification_status"] == TRUTH_UNPROVEN
    assert projection["published_url"] is None
    assert "open_published_url" not in projection["allowed_actions"]
    assert "pause_week" not in projection["allowed_actions"]
    assert projection["next_schedule_at"] is None


def test_engine_adapter_ok_stays_unproven_on_the_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    decisions = tmp_path / "output" / "editorial_decisions"
    publishing = tmp_path / "output" / "publishing"
    publishing.mkdir(parents=True)
    decisions.mkdir(parents=True)
    record = {
        "schema_version": 1,
        "decision_id": "dec-test-1",
        "content_id": "W12",
        "phase": "2b",
        "decision": "approve",
        "approver": "Editor Human",
        "promotion": {"ok": True},
        "authorizes_publish": False,
    }
    (decisions / "decisions.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    monkeypatch.setattr(ea, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ea, "DECISIONS_DIR", decisions)
    monkeypatch.setattr(ea, "HISTORY_JSONL", decisions / "decisions.jsonl")
    monkeypatch.setattr(pe, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(pe, "PUBLISHING_DIR", publishing)
    monkeypatch.setattr(pe, "JOBS_JSONL", publishing / "jobs.jsonl")
    monkeypatch.setattr(pe, "AUDIT_JSONL", publishing / "audit.jsonl")
    monkeypatch.setattr(pe, "JOBS_DIR", publishing / "jobs")
    bind_attempt_ledger(monkeypatch, tmp_path, pe)
    _week_tree(tmp_path, status="published")
    job = pe.create_publish_job(
        content_id="W12",
        channel="website",
        requested_by="Human A",
        tenant_id="tenant-a",
    )
    result = pe.manual_publish(job["job_id"], requested_by="Human A")
    assert result["adapter_result"]["ok"] is True
    assert result["adapter_result"]["rendering_performed"] is False
    assert result["publication_truth"] == truth.TRUTH_UNPROVEN
    projection = _projection(tmp_path)
    assert projection["publication_truth"] == TRUTH_UNPROVEN
    assert projection["published_url"] is None


def test_unknown_remote_retry_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    publishing = tmp_path / "output" / "publishing"
    publishing.mkdir(parents=True)
    monkeypatch.setattr(pe, "JOBS_DIR", publishing / "jobs")
    monkeypatch.setattr(pe, "PUBLISHING_DIR", publishing)
    monkeypatch.setattr(pe, "JOBS_JSONL", publishing / "jobs.jsonl")
    monkeypatch.setattr(pe, "AUDIT_JSONL", publishing / "audit.jsonl")
    job = {
        "job_id": "pub_unknown",
        "bundle_id": "W12",
        "state": "failed",
        "channel": "website",
        "publication_truth": truth.TRUTH_UNKNOWN_REMOTE,
        "remote_write_outstanding": True,
        "retry_count": 0,
        "errors": ["timeout"],
    }
    (publishing / "jobs").mkdir()
    (publishing / "jobs" / "pub_unknown.json").write_text(
        json.dumps(job), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unknown"):
        pe.retry_job("pub_unknown", requested_by="Human A")


def test_anonymous_current_week_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RUNNER_API_KEY", raising=False)

    def _forbidden() -> dict[str, Any]:
        raise AssertionError("anonymous read must not load a week")

    monkeypatch.setattr("runner_api_routers.content_ops.read_current_week", _forbidden)
    response = client.get(
        "/api/v1/content-ops/weeks/current",
        params={"week_id": "W99", "organization_id": str(uuid.uuid4())},
    )
    assert response.status_code == 401


def test_service_principal_cannot_read_current_week(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNNER_API_KEY", _SERVICE_KEY)

    def _forbidden() -> dict[str, Any]:
        raise AssertionError("service read must not load a week")

    monkeypatch.setattr("runner_api_routers.content_ops.read_current_week", _forbidden)
    response = client.get(
        "/api/v1/content-ops/weeks/current",
        headers={"Authorization": f"Bearer {_SERVICE_KEY}"},
        params={"approver": _NAME, "requested_by": _NAME, "organization_id": str(uuid.uuid4())},
    )
    assert response.status_code == 403


def _pin_member(
    client: TestClient,
    identity_db: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
    *,
    member: bool,
) -> None:
    monkeypatch.delenv("RUNNER_API_KEY", raising=False)
    monkeypatch.setenv("FOUNDER_OS_REQUIRE_LOGIN", "1")
    db = identity_db()
    try:
        beta = _org(db, "beta")
        user, email = _user(db)
        if member:
            _member(db, user, beta)
        else:
            other = _org(db, "other")
            _member(db, user, other)
        db.commit()
        beta_id = str(beta.id)
    finally:
        db.close()
    monkeypatch.setenv(CONTENT_OPS_BETA_ORGANIZATION_ENV, beta_id)
    _login(client, email)


def test_valid_human_reads_server_week(
    client: TestClient,
    identity_db: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _week_tree(tmp_path)
    _write_job(tmp_path, publication_truth=TRUTH_UNPROVEN, state="published")
    monkeypatch.setattr(current_week, "REPO_ROOT", tmp_path)
    _pin_member(client, identity_db, monkeypatch, member=True)
    spoof = str(uuid.uuid4())
    response = client.get(
        "/api/v1/content-ops/weeks/current",
        params={
            "week_id": "W99",
            "organization_id": spoof,
            "allowed_actions": "pause_week,approve",
        },
        headers={"X-Organization-Id": spoof},
        cookies={"founder_os_organization": spoof},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["projection"]["week_id"] == "W12"
    assert body["projection"]["publication_truth"] == TRUTH_UNPROVEN
    assert "pause_week" not in body["projection"]["allowed_actions"]
    assert spoof not in response.text


def test_human_non_member_denied(
    client: TestClient,
    identity_db: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_member(client, identity_db, monkeypatch, member=False)
    response = client.get("/api/v1/content-ops/weeks/current")
    assert response.status_code == 403


def test_zero_membership_denied(
    client: TestClient,
    identity_db: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNNER_API_KEY", raising=False)
    db = identity_db()
    try:
        beta = _org(db, "beta")
        _user_row, email = _user(db)
        db.commit()
        beta_id = str(beta.id)
    finally:
        db.close()
    monkeypatch.setenv(CONTENT_OPS_BETA_ORGANIZATION_ENV, beta_id)
    _login(client, email)
    assert client.get("/api/v1/content-ops/weeks/current").status_code == 403


def test_multi_org_unpinned_denied(
    client: TestClient,
    identity_db: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNNER_API_KEY", raising=False)
    monkeypatch.delenv(CONTENT_OPS_BETA_ORGANIZATION_ENV, raising=False)
    db = identity_db()
    try:
        first = _org(db, "one")
        second = _org(db, "two")
        user, email = _user(db)
        _member(db, user, first)
        db.add(
            OrganizationMembership(
                user_id=user.id,
                organization_id=second.id,
                role="owner",
                status=MembershipStatus.ACTIVE,
            )
        )
        db.commit()
    finally:
        db.close()
    _login(client, email)
    assert client.get("/api/v1/content-ops/weeks/current").status_code == 403


def test_cross_tenant_write_denied(
    client: TestClient,
    identity_db: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_member(client, identity_db, monkeypatch, member=False)
    response = client.post(
        "/api/v1/editorial/W12/approve",
        json={"approver": _NAME, "notes": "spoof", "phase": "2b", "organization_id": str(uuid.uuid4())},
    )
    assert response.status_code == 403
