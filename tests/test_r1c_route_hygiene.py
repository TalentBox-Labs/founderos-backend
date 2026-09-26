"""R1C route/template hygiene focused tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    from runner_api import app
    from runner_api_routers.utils import _verify_api_key

    async def _ok(_: str | None = None) -> str:
        return "test-key"

    app.dependency_overrides[_verify_api_key] = _ok
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_marketing_page_renders_without_500(client: TestClient) -> None:
    r = client.get("/marketing")
    assert r.status_code == 200
    assert "Marketing Agent" in r.text
    assert "integration_status" not in r.text or "Connected" in r.text or "Not configured" in r.text


def test_health_remains_api_json(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("application/json")
    assert r.json().get("status") == "ok"


def test_health_not_in_sidebar_nav(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert 'href="/health"' not in r.text


def test_pipeline_page_omits_unimplemented_stage_endpoints(client: TestClient) -> None:
    r = client.get("/pipeline")
    assert r.status_code == 200
    assert "qa:          '/qa'" not in r.text
    assert "'sheet-sync':'/sheet-sync'" not in r.text
    assert "CrewAI QA" not in r.text
    assert "Sheet Sync" not in r.text


def test_week_detail_omits_qa_report_route(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from runner_api_routers import utils as u

    monkeypatch.setattr(
        u,
        "_read_tracker",
        lambda: [
            {
                "content_id": "W01",
                "title": "Sample",
                "current_step": "draft",
                "final_output_path": "",
            }
        ],
    )
    monkeypatch.setattr(u, "_week_artifacts", lambda _wid: {})
    monkeypatch.setattr(u, "_load_runtime", lambda: {"active_week": "W01"})
    r = client.get("/weeks/W01")
    assert r.status_code == 200
    assert "/weeks/W01/qa-report" not in r.text


def test_marketing_omits_run_detail_route(client: TestClient) -> None:
    r = client.get("/marketing")
    assert r.status_code == 200
    assert "/marketing/run/" not in r.text


def test_app_crm_optional_when_dist_absent(client: TestClient) -> None:
    """React CRM /app is optional; without frontend/dist expect explicit 503."""
    from pathlib import Path

    dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if dist.is_dir():
        pytest.skip("frontend/dist present in this worktree; 503 path not applicable")
    r = client.get("/app")
    assert r.status_code == 503
    body = r.json()
    assert body.get("ok") is False
    assert body.get("status") == "not_built"
    assert body.get("service") == "crm_frontend"


def test_broken_qa_report_route_404(client: TestClient) -> None:
    r = client.get("/weeks/W01/qa-report")
    assert r.status_code == 404


def test_orchestration_run_uses_jinja_template(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json

    import revenue_os.services.go_to_market_orchestrator as gtm

    run_id = "r1c_test_run"
    audit_dir = tmp_path / "orchestration_runs"
    audit_dir.mkdir()
    payload = {
        "run_id": run_id,
        "timestamp": "2026-08-12T00:00:00Z",
        "backend": "hermes",
        "brand": "workcrew",
        "keyword": "test",
        "topic": "topic",
        "geo_target": "global",
        "funnel_stage": "consideration",
        "audience": "ops",
        "events": [],
        "results": {"executions": {"seo_backend": {"status": "ok"}}, "audit": {"run_file": "x.json"}},
    }
    (audit_dir / f"{run_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(gtm, "_audit_dir", lambda: audit_dir)

    r = client.get(f"/orchestration/run/{run_id}")
    assert r.status_code == 200
    assert "Orchestration Run" in r.text
    assert "Per-Channel Events" in r.text
    assert "Back to Marketing" in r.text  # from Jinja base/topbar, not bare inline page
