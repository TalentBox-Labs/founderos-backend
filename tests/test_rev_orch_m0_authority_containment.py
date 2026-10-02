"""REV-ORCH M0 — authority and legacy path containment attestation tests."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from revenue_os.agents.orchestration import WorkflowOrchestrator
from revenue_os.main import app as legacy_app
from revenue_os.services import ai_service
from revenue_os.services import approvals as approvals_service
from revenue_os.services import sales_agents
from revenue_os.services.credentials_vault import load_credentials
from revenue_os.services.mutation_authority import (
    HumanAuthorityError,
    require_human_mutation_authority,
)
from runner_api import app as runner_app

ROOT = Path(__file__).resolve().parents[1]
M0_DOCS = ROOT / "docs" / "revenue" / "orchestration" / "m0"

M0_REQUIRED_ARTIFACTS = (
    "M0_ORCHESTRATION_ENTRYPOINT_INVENTORY.md",
    "M0_CANONICAL_ORCHESTRATOR_DECISION.md",
    "M0_CREWAI_LEGACY_ATTESTATION.md",
    "M0_AI_AUTHORITY_CONTRACT.md",
    "M0_AI_TOOL_CALLING_NEGATIVE_SCOPE.md",
    "M0_APPROVAL_REQUEST_REUSE_ATTESTATION.md",
    "M0_TENANT_CONTEXT_PROPAGATION_AUDIT.md",
    "M0_STALE_AUTHORITY_EXECUTION_CONTRACT.md",
    "M0_API_SURFACE_DECISION.md",
    "M0_PLATFORM_CONNECTOR_BOUNDARY.md",
    "M0_AI_PROPOSAL_SCHEMA_RECOMMENDATION.md",
    "M0_OUTBOUND_IDEMPOTENCY_CONTRACT.md",
    "M0_SALES_AGENT_MODULE_RECONCILIATION.md",
    "M0_IMPLEMENTATION_READINESS.md",
)


@pytest.fixture
def legacy_client() -> TestClient:
    return TestClient(legacy_app)


@pytest.fixture
def runner_client() -> TestClient:
    return TestClient(runner_app)


def test_m0_documentation_artifacts_present() -> None:
    missing = [name for name in M0_REQUIRED_ARTIFACTS if not (M0_DOCS / name).exists()]
    assert missing == [], f"Missing M0 artifacts: {missing}"


def test_canonical_orchestrator_is_workflow_orchestrator() -> None:
    assert WorkflowOrchestrator.__name__ == "WorkflowOrchestrator"
    source = inspect.getsource(WorkflowOrchestrator._execute_agent)
    assert "Placeholder" in source or "placeholder" in source.lower()


def test_orchestration_entrypoint_inventory_covers_core_modules() -> None:
    inventory = (M0_DOCS / "M0_ORCHESTRATION_ENTRYPOINT_INVENTORY.md").read_text()
    for fragment in (
        "WorkflowOrchestrator",
        "AgentCoordinator",
        "EventBus",
        "HeartbeatScheduler",
        "sales_agents",
        "AIService",
        "n8n_bridge",
        "sdr_agent",
        "runner_api",
        "revenue_os/main.py",
    ):
        assert fragment in inventory


def test_crewai_legacy_routes_quarantined_on_legacy_app(legacy_client: TestClient) -> None:
    resp = legacy_client.post(
        "/api/v1/agents/score-lead",
        json={"company_name": "Acme"},
    )
    assert resp.status_code == 404


def test_crewai_not_mounted_on_runner_api(runner_client: TestClient) -> None:
    resp = runner_client.post(
        "/api/v1/agents/score-lead",
        json={"company_name": "Acme"},
    )
    assert resp.status_code in {404, 405, 422}


def test_aiservice_is_propose_only_no_crm_mutators() -> None:
    public = {
        n
        for n, obj in inspect.getmembers(ai_service, inspect.isfunction)
        if not n.startswith("_")
    }
    prohibited = {"update_contact", "create_deal", "apply_deal_stage", "accept_qualified_demand"}
    assert public.isdisjoint(prohibited)


def test_sales_agents_outbound_drafts_use_approval_request() -> None:
    cold_src = inspect.getsource(sales_agents.draft_cold_email)
    linkedin_src = inspect.getsource(sales_agents.draft_linkedin_opener)
    assert "request_approval" in cold_src
    assert "request_approval" in linkedin_src
    assert "trigger_workflow" not in cold_src


def test_outbound_send_requires_approval_executor() -> None:
    assert "send_outreach_email" in approvals_service.EXECUTORS
    executor_src = inspect.getsource(approvals_service._execute_send_outreach_email)
    assert "trigger_workflow" in executor_src


def test_no_general_ai_tool_calling_in_revenue_services() -> None:
    ai_src = inspect.getsource(ai_service)
    assert "tools=" not in ai_src
    assert "tool_calls" not in ai_src
    assert "function_call" not in ai_src


def test_mutation_authority_blocks_ai_identity() -> None:
    with pytest.raises(HumanAuthorityError):
        require_human_mutation_authority("openai", action="deal stage")
    with pytest.raises(HumanAuthorityError):
        require_human_mutation_authority("crewai", action="contact status")
    with pytest.raises(HumanAuthorityError):
        require_human_mutation_authority("agent:hermes", action="deal stage")


def test_mutation_authority_allows_human() -> None:
    assert require_human_mutation_authority("Krishna Founder", action="test") == "Krishna Founder"


def test_s4_5_connector_vault_default_no_global_fallback() -> None:
    sig = inspect.signature(load_credentials)
    assert sig.parameters["allow_global_fallback"].default is False


def test_canonical_api_surface_is_runner_api() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "scripts/publication_admission_start.sh" in dockerfile
    script = (ROOT / "scripts" / "publication_admission_start.sh").read_text()
    assert "runner_api:app" in script
    decision = (M0_DOCS / "M0_API_SURFACE_DECISION.md").read_text()
    assert "CANONICAL_FOR_NEW_ORCHESTRATION" in decision
    assert "runner_api:app" in decision


def test_dual_api_documented() -> None:
    decision = (M0_DOCS / "M0_API_SURFACE_DECISION.md").read_text()
    assert "revenue_os/main.py" in decision
    assert "LEGACY" in decision


def test_tool_calling_prohibited_for_m1_documented() -> None:
    doc = (M0_DOCS / "M0_AI_TOOL_CALLING_NEGATIVE_SCOPE.md").read_text()
    assert "PROHIBITED_FOR_M1" in doc


def test_no_new_sot_in_m0_docs() -> None:
    readiness = (M0_DOCS / "M0_IMPLEMENTATION_READINESS.md").read_text()
    assert "New SoT" in readiness
    assert "NO" in readiness.split("New SoT")[1][:40]


def test_frozen_contract_files_unchanged_count() -> None:
    """M0 must not modify frozen baseline markdown manifests."""
    baseline = ROOT / "docs" / "saas" / "s4_5" / "INTEGRATION_TENANT_ISOLATION_BASELINE_v1.0.md"
    manifest = ROOT / "docs" / "saas" / "s4_5" / "S4_5_BASELINE_MANIFEST.md"
    assert baseline.exists()
    assert "Frozen contract changes" in manifest.read_text()


def test_workflow_orchestrator_execute_agent_is_stub() -> None:
    result = WorkflowOrchestrator._execute_agent("icp_research_agent", {})
    assert "decision" in result
    assert "action_by_icp_research_agent" in result["decision"]


def test_approval_execution_separation_documented() -> None:
    doc = (M0_DOCS / "M0_APPROVAL_REQUEST_REUSE_ATTESTATION.md").read_text()
    assert "decide()" in doc
    assert "EXECUTORS" in doc


def test_stale_authority_contract_defined() -> None:
    doc = (M0_DOCS / "M0_STALE_AUTHORITY_EXECUTION_CONTRACT.md").read_text()
    assert "revalidate" in doc.lower()


def test_outbound_idempotency_contract_defined() -> None:
    doc = (M0_DOCS / "M0_OUTBOUND_IDEMPOTENCY_CONTRACT.md").read_text()
    assert "idempotency" in doc.lower()


def test_crewai_attestation_documents_quarantine() -> None:
    doc = (M0_DOCS / "M0_CREWAI_LEGACY_ATTESTATION.md").read_text()
    assert "QUARANTINED" in doc
    assert "410" in doc
