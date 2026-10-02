"""Enterprise shell: live reads, explicit gaps, no publication inference."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.ui.enterprise.catalog import API_GAPS, resolve_surface
from src.ui.enterprise.compose import _publication_label, compose_surface

SURFACES = (
    "/home",
    "/os/revenue",
    "/os/revenue/contacts",
    "/os/revenue/companies",
    "/os/revenue/deals",
    "/os/revenue/pipeline",
    "/os/revenue/activities",
    "/os/revenue/prospecting",
    "/os/revenue/intelligence",
    "/os/marketing",
    "/os/marketing/campaigns",
    "/os/marketing/demand",
    "/os/marketing/seo",
    "/os/marketing/performance",
    "/os/marketing/analytics",
    "/os/content",
    "/os/content/calendar",
    "/os/content/studio",
    "/os/content/editorial",
    "/os/content/publishing",
    "/os/content/seo",
    "/os/content/pipeline",
    "/os/content/analytics",
    "/os/operations",
    "/os/operations/approvals",
    "/os/operations/activity",
    "/os/operations/automations",
    "/os/operations/jobs",
    "/os/agents",
    "/os/agents/runs",
    "/os/agents/capabilities",
    "/os/agents/evaluations",
    "/os/system",
    "/os/system/integrations",
    "/os/system/governance",
    "/os/system/identity",
    "/os/system/tenant",
    "/os/system/health",
    "/os/system/settings",
    "/os/design-system",
)


def test_unknown_workspace_is_not_a_fixture(cms_client: TestClient) -> None:
    response = cms_client.get("/os/revenue/not-a-section")
    assert response.status_code == 404
    assert resolve_surface("revenue", "not-a-section") is None


def test_shell_routes_render_without_mock_fallback(cms_client: TestClient) -> None:
    for path in SURFACES:
        response = cms_client.get(path)
        assert response.status_code == 200, path
        html = response.text
        assert 'data-testid="os-surface"' in html, path
        assert 'data-source="mock"' not in html, path
        assert "Acme" not in html, path
        assert 'data-testid="enterprise-nav"' in html
        assert 'href="/command"' in html
        assert 'href="/demand"' in html
        assert 'href="/pending-approvals"' in html
        assert 'href="/activity"' in html
        assert 'href="/operator"' in html
        assert "Command Center" in html
        assert 'data-testid="env-indicator"' in html


def test_companies_workspace_states_the_gap(cms_client: TestClient) -> None:
    html = cms_client.get("/os/revenue/companies").text
    assert "API_GAP" in html
    assert 'data-read-state="gap"' in html
    assert "No companies endpoint is available" in html
    assert API_GAPS[0] in html


def test_content_workspace_does_not_claim_verified_publication(cms_client: TestClient) -> None:
    html = cms_client.get("/os/content").text
    assert "Verified published" in html
    assert "QA pass is not publication." in html
    assert "Publication truth" in html
    assert "mock contract" not in html.lower()
    assert 'data-source="mock"' not in html
    assert "PUBLISHED / VERIFIED" not in html
    publishing = cms_client.get("/os/content/publishing").text
    assert "not canonical publication truth" in publishing
    assert "PUBLISHED / VERIFIED" not in publishing
    assert 'data-source="mock"' not in publishing


def test_published_job_state_is_not_verified_publication() -> None:
    label = _publication_label("published")
    assert "Verified" not in label
    assert "unknown" in label.lower()


def test_hermes_is_status_only(cms_client: TestClient) -> None:
    html = cms_client.get("/os/agents").text
    assert 'data-testid="hermes-status"' in html
    assert 'data-authority="none"' in html
    assert "does not grant Hermes execution authority" in html or "has no authority from this screen" in html


def test_gmail_stays_blocked_on_integrations(cms_client: TestClient) -> None:
    html = cms_client.get("/os/system/integrations").text
    assert "Gmail blocked" in html
    assert "cannot enable it" in html


def test_automations_are_not_listed_across_tenants(cms_client: TestClient) -> None:
    html = cms_client.get("/os/operations/automations").text
    assert 'data-read-state="gap"' in html
    assert "process-global" in html


def test_content_section_stays_open_on_content_workspace(cms_client: TestClient) -> None:
    html = cms_client.get("/os/content").text
    assert 'data-testid="content-ops-nav"' in html
    assert "open" in html[html.find('data-testid="content-ops-nav"') - 80 : html.find('data-testid="content-ops-nav"') + 40]


def test_prospecting_does_not_execute_outreach(cms_client: TestClient) -> None:
    html = cms_client.get("/os/revenue/prospecting").text
    assert "does not source, import, or send outreach" in html
    assert "Import Selected" not in html
    assert "Execute Free Stage" not in html
    assert 'href="/sales"' in html


def test_content_seo_does_not_invent_a_readiness_score(cms_client: TestClient) -> None:
    html = cms_client.get("/os/content/seo").text
    assert "SEO readiness is not scored here" in html
    assert ">Unknown<" in html or "Unknown" in html
    assert "score: 100" not in html.lower()


def test_content_pipeline_does_not_start_a_run(cms_client: TestClient) -> None:
    html = cms_client.get("/os/content/pipeline").text
    assert "does not start a run" in html
    assert "Verified published" in html
    assert "Publication truth" in html


def test_compose_without_organization_does_not_invent_contacts() -> None:
    view = compose_surface(
        "revenue_contacts",
        organization_id=None,
        identity=None,
        tenant=None,
        active_week="—",
    )
    assert view["read_state"] == "unavailable"
    assert view["table"] is None
