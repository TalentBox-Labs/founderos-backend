"""HTTP runner for n8n — requires ``pip install -r requirements-api.txt``."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from runner_api import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


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


def test_run_pipeline_requires_bearer_when_env_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNNER_API_KEY", "test-secret-token")
    r = client.post("/run-pipeline", json={})
    assert r.status_code == 401

    ok = MagicMock()
    ok.returncode = 0
    ok.stdout = ""
    ok.stderr = ""
    with patch("runner_api_routers.pipeline._run", return_value=ok):
        r2 = client.post(
            "/run-pipeline",
            json={},
            headers={"Authorization": "Bearer test-secret-token"},
        )
    assert r2.status_code == 200
    assert r2.json()["status"] == "ok"
