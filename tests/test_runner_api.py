"""HTTP runner for n8n — requires ``pip install -r requirements-api.txt``."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import sessionmaker

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import revenue_os.services.tenant_resolution as tenant_resolution_mod
import runner_api_routers.identity as identity_mod
from revenue_os.auth import hash_password
from revenue_os.database import engine
from revenue_os.models.organization import (
    MembershipStatus,
    Organization,
    OrganizationMembership,
    OrganizationStatus,
)
from revenue_os.models.user import User
from revenue_os.services.content_ops_authority import CONTENT_OPS_BETA_ORGANIZATION_ENV
from runner_api import app
from runner_api_routers.utils import _verify_api_key, require_human_or_api_key

_PIPELINE_PASSWORD = "RunPipelineExpectation1!"
_PIPELINE_HUMAN = "Pipeline Reviewer"


@pytest.fixture
def client() -> TestClient:
    async def _ok(request=None, credentials=None):
        return "test-key"

    app.dependency_overrides[_verify_api_key] = _ok
    app.dependency_overrides[require_human_or_api_key] = _ok
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(_verify_api_key, None)
        app.dependency_overrides.pop(require_human_or_api_key, None)


def test_health_returns_ok(client: TestClient) -> None:
    """Canonical GET /health is process liveness only (database not checked)."""
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["service"] == "WorkCrew CMS OS"
    assert data["check"] == "liveness"
    assert data["database"] == "not_checked"
    assert "project_root" not in data


def test_health_ready_reports_database(client: TestClient) -> None:
    r = client.get("/health/ready")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["check"] == "readiness"
    assert data["database"] == "ok"


def test_health_debug_returns_diagnostics(client: TestClient) -> None:
    """GET /health/debug exposes process diagnostics for operators."""
    r = client.get("/health/debug")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["service"] == "WorkCrew CMS OS"
    assert data["database"] == "not_checked"
    assert "project_root" in data
    assert "python" in data


def test_sales_page_contains_prospecting_panel(client: TestClient) -> None:
    template_path = Path(__file__).resolve().parent.parent / "templates" / "sales.html"
    text = template_path.read_text(encoding="utf-8")
    assert "Sales Prospecting" in text
    assert "Build Plan" in text
    assert "Saved Presets" in text


def test_run_pipeline_no_week_only_orchestrator(client: TestClient) -> None:
    """POST /run-pipeline without week runs pipeline_orchestrator; status is ok|failed."""
    ok = subprocess.CompletedProcess(
        ["python", "-m", "src.tools.pipeline_orchestrator"],
        returncode=0,
        stdout="done\n",
        stderr="",
    )
    with patch("runner_api_routers.pipeline._run", return_value=ok) as m:
        r = client.post("/run-pipeline", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["week"] is None
    assert len(body["steps"]) == 1
    assert body["steps"][0]["step"] == "pipeline_orchestrator"
    assert body["steps"][0]["returncode"] == 0
    assert len(m.call_args_list) == 1
    assert m.call_args_list[0][0][0][-1] == "src.tools.pipeline_orchestrator"


def test_run_pipeline_with_week_runs_apply_then_orchestrator(client: TestClient) -> None:
    apply_ok = subprocess.CompletedProcess(
        [], returncode=0, stdout="Applied\n", stderr=""
    )
    main_ok = subprocess.CompletedProcess(
        [], returncode=0, stdout="ALL VALIDATION GATES PASSED\n", stderr=""
    )
    with patch(
        "runner_api_routers.pipeline._run", side_effect=[apply_ok, main_ok]
    ) as m:
        r = client.post("/run-pipeline", json={"week": "w10", "topic": "t"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["week"] == "W10"
    assert len(m.call_args_list) == 2
    first_cmd = m.call_args_list[0][0][0]
    assert first_cmd[-1] == "W10"
    assert "src.tools.runtime_apply" in first_cmd
    assert m.call_args_list[1][0][0][-1] == "src.tools.pipeline_orchestrator"


def test_run_pipeline_stops_when_runtime_apply_fails(client: TestClient) -> None:
    fail = subprocess.CompletedProcess([], returncode=1, stdout="", stderr="no profile")
    with patch("runner_api_routers.pipeline._run", return_value=fail):
        r = client.post("/run-pipeline", json={"week": "ZZ99"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert len(body["steps"]) == 1
    assert body["steps"][0]["returncode"] == 1


def _pipeline_identity_db(monkeypatch: pytest.MonkeyPatch) -> sessionmaker:
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(identity_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(tenant_resolution_mod, "SessionLocal", session_factory)
    return session_factory


def _pipeline_user(db, *, name: str) -> tuple[User, str]:
    email = f"pipeline-{uuid.uuid4().hex}@talentbox.invalid"
    user = User(
        email=email,
        hashed_password=hash_password(_PIPELINE_PASSWORD),
        full_name=name,
        role="owner",
        is_active=1,
    )
    db.add(user)
    db.flush()
    return user, email


def _login_pipeline_human(client: TestClient, email: str) -> None:
    response = client.post(
        "/api/v1/identity/login",
        json={"email": email, "password": _PIPELINE_PASSWORD},
    )
    assert response.status_code == 200, response.text


@pytest.mark.real_api_auth
def test_run_pipeline_requires_human_beta_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy pipeline mutation stays human-beta-tenant only.

    A RUNNER_API_KEY bearer authenticates SERVICE. It does not become HUMAN,
    including when the body carries a human-looking name.
    """
    app.dependency_overrides.pop(_verify_api_key, None)
    app.dependency_overrides.pop(require_human_or_api_key, None)
    monkeypatch.setenv("RUNNER_API_KEY", "test-secret-token")
    session_factory = _pipeline_identity_db(monkeypatch)
    db = session_factory()
    try:
        org = Organization(
            name="Pipeline Beta",
            slug=f"pipeline-beta-{uuid.uuid4().hex[:8]}",
            status=OrganizationStatus.ACTIVE,
        )
        db.add(org)
        db.flush()
        _outsider, outsider_email = _pipeline_user(db, name=_PIPELINE_HUMAN)
        member, member_email = _pipeline_user(db, name=_PIPELINE_HUMAN)
        db.add(
            OrganizationMembership(
                user_id=member.id,
                organization_id=org.id,
                role="owner",
                status=MembershipStatus.ACTIVE,
            )
        )
        db.commit()
        beta_id = str(org.id)
    finally:
        db.close()
    monkeypatch.setenv(CONTENT_OPS_BETA_ORGANIZATION_ENV, beta_id)

    client = TestClient(app)
    anonymous = client.post("/run-pipeline", json={})
    assert anonymous.status_code == 401

    ok = MagicMock()
    ok.returncode = 0
    ok.stdout = ""
    ok.stderr = ""
    service_headers = {"Authorization": "Bearer test-secret-token"}
    human_looking = {
        "topic": _PIPELINE_HUMAN,
        "requested_by": _PIPELINE_HUMAN,
        "approver": _PIPELINE_HUMAN,
    }
    with patch("runner_api_routers.pipeline._run", return_value=ok) as run:
        service = client.post("/run-pipeline", json={}, headers=service_headers)
        named = client.post(
            "/run-pipeline",
            json=human_looking,
            headers=service_headers,
        )
        assert run.call_count == 0

        client.cookies.clear()
        _login_pipeline_human(client, outsider_email)
        non_member = client.post("/run-pipeline", json={})
        assert run.call_count == 0

        client.cookies.clear()
        _login_pipeline_human(client, member_email)
        member_response = client.post("/run-pipeline", json={})

    assert service.status_code == 403
    assert named.status_code == 403
    assert non_member.status_code == 403
    assert member_response.status_code == 200
    assert member_response.json()["status"] == "ok"
    assert run.call_count == 1
