"""OF1.5 — Founder OS Operator Flow Baseline v1.0 freeze suite."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import runner_api_routers.cockpit as cockpit_mod
import runner_api_routers.operator_flow as of_router
import runner_api_routers.ui as ui_mod
import tests.test_a3_runner_deal_stage as a3
import tests.test_a4_runner_contact_status as a4
import tests.test_mc04_qualified_demand as mc04
import tests.test_mc06_5_commercial_outcome_baseline_freeze as mc06_5
import tests.test_of1_operator_flow as of1
from tests.test_of1_operator_flow import human_session
import tests.test_sales_api_runner as a1
import tests.test_ui2_5_cockpit_baseline_freeze as ui25
from revenue_os.services import operator_flow_read_model as of_read
from runner_api import app
from tests.test_r1c_route_hygiene import test_app_crm_optional_when_dist_absent

ROOT = Path(__file__).resolve().parents[1]

FROZEN_OPERATOR_POST_ROUTES = frozenset(
    {
        "/api/v1/operator/actions/qualified-demand/accept",
        "/api/v1/operator/actions/qualified-demand/reject",
        "/api/v1/operator/actions/contact-status",
        "/api/v1/operator/actions/deal/create",
        "/api/v1/operator/actions/deal/stage",
        "/api/v1/operator/actions/commercial-outcome/handoff",
        "/api/v1/operator/actions/commercial-outcome/accept",
        "/api/v1/operator/actions/commercial-outcome/reject",
    }
)

FROZEN_MIGRATION_HEAD = "e8278e1169e6_full_schema.py"

A1_5_KNOWN_EXCEPTIONS = (
    "tests/test_prospecting_ui.py::test_prospecting_plan_endpoint_returns_stage_allocation_and_leads",
    "tests/test_prospecting_ui.py::test_prospecting_plan_validation_error_for_invalid_score",
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def operator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDER_OS_OPERATOR_NAME", of1._OPERATOR)


def _operator_html() -> str:
    return (ROOT / "templates" / "operator.html").read_text(encoding="utf-8")


def _router_source() -> str:
    return inspect.getsource(of_router)


def _read_model_source() -> str:
    return inspect.getsource(of_read)


def _scope_doc() -> str:
    return (ROOT / "docs" / "operator" / "of1_5" / "OPERATOR_VALUE_CHAIN_SCOPE_v1.0.md").read_text(
        encoding="utf-8"
    )


def _baseline_doc() -> str:
    return (ROOT / "docs" / "operator" / "of1_5" / "OPERATOR_FLOW_BASELINE_v1.0.md").read_text(
        encoding="utf-8"
    )


def _full_route_path(route: object, prefix: str) -> str:
    path = getattr(route, "path", "")
    if prefix and not path.startswith(prefix):
        return f"{prefix}{path}"
    return path


def test_freeze_operator_route_available(client: TestClient) -> None:
    r = client.get("/operator")
    assert r.status_code == 200
    assert "Operator Flow" in r.text


def test_freeze_canonical_founder_os_shell(client: TestClient) -> None:
    html = _operator_html()
    assert '{% extends "base.html" %}' in html
    r = client.get("/operator")
    assert r.status_code == 200
    assert "Founder OS" in r.text
    assert "react" not in r.text.lower()
    assert "createRoot" not in r.text


def test_freeze_bounded_workflow_topology() -> None:
    html = _operator_html()
    for marker in (
        'data-testid="stage-demand"',
        'data-testid="stage-qualification"',
        'data-testid="stage-deals"',
        'data-testid="stage-outcome"',
        'data-testid="stage-revenue"',
    ):
        assert marker in html
    src = _read_model_source()
    assert "pending_demands" in src
    assert "contacts" in src
    assert "deals" in src
    assert "outcomes" in src or "commercial" in src.lower()


def test_freeze_qualified_demand_to_contact(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None, human_session: str
) -> None:
    of1.test_path_a_qualified_demand_accept(client, monkeypatch, operator_env, human_session)


def test_freeze_contact_to_deal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None
) -> None:
    of1.test_deal_create_links_contact(client, monkeypatch, operator_env)


def test_freeze_deal_to_closed_won(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None
) -> None:
    of1.test_path_c_valid_deal_stage(client, monkeypatch, operator_env)


def test_freeze_closed_won_to_commercial_outcome(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None
) -> None:
    of1.test_path_d_eligible_handoff(client, monkeypatch, operator_env)


def test_freeze_commercial_outcome_to_revenue_decision(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None
) -> None:
    of1.test_path_e_revenue_accept_and_idempotent(client, monkeypatch, operator_env)


def test_freeze_trusted_human_authority_required(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FOUNDER_OS_OPERATOR_NAME", raising=False)
    r = client.post(
        "/api/v1/operator/actions/deal/stage",
        json={"deal_id": of1._DEAL_ID, "stage": "closed_won"},
    )
    assert r.status_code == 503
    src = _router_source()
    assert src.count("_trusted_cockpit_operator()") == 8
    for model in (
        of_router.DemandDecisionBody,
        of_router.ContactStatusBody,
        of_router.DealCreateBody,
        of_router.DealStageBody,
        of_router.OutcomeHandoffBody,
        of_router.OutcomeDecisionBody,
    ):
        assert "requested_by" not in model.model_fields


def test_freeze_agent_mutation_blocked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    of1.test_agent_mutation_rejected(client, monkeypatch)


def test_freeze_ai_mutation_blocked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    of1.test_ai_mutation_rejected(client, monkeypatch)


def test_freeze_spoofed_human_metadata_blocked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None, human_session: str
) -> None:
    of1.test_spoofed_human_identity_ignored(client, monkeypatch, operator_env, human_session)
    assert "requested_by" not in _operator_html()


def test_freeze_direct_service_domain_bypass_blocked() -> None:
    of1.test_ui1_1_service_bypass_still_blocked()
    html = _operator_html()
    assert "/api/v1/crm/" not in html
    assert "accept_qualified_demand" not in html
    assert "apply_deal_stage_update" not in html
    assert "register_commercial_outcome_handoff" not in html
    fetches = [
        line.strip()
        for line in html.splitlines()
        if "ofPost(" in line and "/api/" in line
    ]
    assert fetches
    for line in fetches:
        assert "/api/v1/operator/actions/" in line


def test_freeze_invalid_transitions_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None
) -> None:
    of1.test_invalid_deal_transition_rejected(client, monkeypatch, operator_env)
    of1.test_commercial_outcome_requires_closed_won(client, monkeypatch, operator_env)
    of1.test_deal_create_rejects_closed_won(client, monkeypatch, operator_env)


def test_freeze_audit_integrity(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None, human_session: str
) -> None:
    of1.test_audit_trail_on_accept(client, monkeypatch, operator_env, human_session)


def test_freeze_idempotency_preserved(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None
) -> None:
    of1.test_path_e_revenue_accept_and_idempotent(client, monkeypatch, operator_env)


def test_freeze_duplicate_safety(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None
) -> None:
    of1.test_path_e_revenue_accept_and_idempotent(client, monkeypatch, operator_env)
    of1.test_path_d_eligible_handoff(client, monkeypatch, operator_env)
    db = of1._FakeDB()
    db.deals[0].stage = of1.DealStage.CLOSED_WON
    monkeypatch.setattr(of_router, "SessionLocal", lambda: db)
    first = client.post(
        "/api/v1/operator/actions/commercial-outcome/handoff",
        json={"deal_id": of1._DEAL_ID},
    )
    second = client.post(
        "/api/v1/operator/actions/commercial-outcome/handoff",
        json={"deal_id": of1._DEAL_ID},
    )
    assert first.status_code == 200
    assert second.status_code == 422
    assert "already registered" in second.json()["detail"]


def test_freeze_no_new_shared_sot() -> None:
    assert not (ROOT / "revenue_os" / "models" / "operator_flow.py").exists()
    assert not (ROOT / "revenue_os" / "models" / "operator.py").exists()
    src = _read_model_source()
    assert "Contains no independent business state" in src
    tree = ast.parse(src)
    model_bases = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            model_bases.extend(
                base.id for base in node.bases if isinstance(base, ast.Name)
            )
    assert "Base" not in model_bases
    assert "DeclarativeBase" not in model_bases


def test_freeze_no_database_migration() -> None:
    versions = sorted(
        p.name
        for p in (ROOT / "migrations" / "versions").glob("*.py")
        if p.name != "__init__.py"
    )
    assert versions == [FROZEN_MIGRATION_HEAD]
    src = _router_source() + _read_model_source()
    assert "alembic" not in src.lower()
    assert "CREATE TABLE" not in src


def test_freeze_no_external_integration_dependency() -> None:
    src = _router_source()
    tree = ast.parse(src)
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden = {
        "httpx",
        "requests",
        "stripe",
        "boto3",
        "openai",
        "anthropic",
        "twilio",
        "sendgrid",
    }
    assert imported.isdisjoint(forbidden)
    html = _operator_html()
    assert "https://" not in html


def test_freeze_no_crm_spa_mount() -> None:
    html = _operator_html()
    assert "/app" not in html
    assert "frontend/dist" not in html
    page_src = inspect.getsource(ui_mod.page_operator)
    assert "StaticFiles" not in page_src
    assert "/app" not in page_src
    from pathlib import Path

    dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if dist.is_dir():
        return
    test_app_crm_optional_when_dist_absent(TestClient(app))


def test_freeze_ui2_5_baseline_unchanged() -> None:
    ui25.test_freeze_cockpit_router_only_two_mutations()
    of1.test_cockpit_still_has_only_two_mutations()
    mutation_routes = [
        route.path
        for route in cockpit_mod.router.routes
        if hasattr(route, "methods") and "POST" in route.methods
    ]
    assert sorted(mutation_routes) == [
        "/api/v1/cockpit/actions/contact-status",
        "/api/v1/cockpit/actions/qualified-demand/accept",
    ]


def test_freeze_mc06_5_baseline_unchanged() -> None:
    mc06_5.test_freeze_a3_5_emission_false()
    mc06_5.test_freeze_no_shared_sot()
    mc06_5.test_freeze_eligibility_closed_won_only()


def test_freeze_mc04_5_baseline_unchanged() -> None:
    mc04.test_register_handoff_does_not_create_contact()
    mc04.test_accept_idempotent()


def test_freeze_a4_5_baseline_unchanged() -> None:
    a4.test_score_contact_does_not_mutate_status()
    a4.test_apply_contact_status_update_same_status_noop()


def test_freeze_a3_5_baseline_unchanged() -> None:
    a3.test_apply_deal_stage_update_rejects_reopen()
    a3.test_apply_deal_stage_update_same_stage_noop()
    mc06_5.test_freeze_a3_5_emission_false()


def test_freeze_a1_5_baseline_unchanged() -> None:
    a1.test_sales_template_exists()
    exceptions = (
        ROOT / "docs" / "sales" / "SALES_A1_5_KNOWN_TEST_EXCEPTIONS.md"
    ).read_text(encoding="utf-8")
    for identity in A1_5_KNOWN_EXCEPTIONS:
        assert identity in exceptions
    of1_5_exceptions = (
        ROOT / "docs" / "operator" / "of1_5" / "OF1_5_KNOWN_TEST_EXCEPTIONS.md"
    ).read_text(encoding="utf-8")
    for identity in A1_5_KNOWN_EXCEPTIONS:
        assert identity.split("::", 1)[1] in of1_5_exceptions


def test_freeze_audience_to_demand_outside_of1() -> None:
    src = _router_source()
    assert "register_marketing_handoff" not in src
    assert "marketing/qualified-demand/handoff" not in src
    html = _operator_html()
    assert "marketing/qualified-demand/handoff" not in html
    assert "Audience→Demand" in html
    prefix = of_router.router.prefix
    routes = {
        _full_route_path(route, prefix)
        for route in of_router.router.routes
        if hasattr(route, "methods")
    }
    assert not any("marketing" in path for path in routes)


def test_freeze_value_chain_scope_not_conflated() -> None:
    scope = _scope_doc()
    assert "BOUNDED OPERATOR FLOW" in scope or "Bounded operator flow" in scope
    assert "Demand → Revenue Decision" in scope
    assert "**COMPLETE**" in scope
    assert "Audience → Demand → Revenue Decision" in scope
    assert "**PARTIAL**" in scope
    assert "Audience → Demand" in scope
    assert "MUST NOT treat" in scope or "must not" in scope.lower()
    assert "OF1.5-SCOPE-001" in scope
    html = _operator_html()
    assert "no Audience→Demand capture" in html


def test_freeze_bounded_operator_flow_complete() -> None:
    baseline = _baseline_doc()
    assert "Bounded operator flow (Demand → Revenue Decision): COMPLETE" in baseline
    assert "Total Founder OS value chain" in baseline
    assert "PARTIAL" in baseline
    topology = (
        ROOT / "docs" / "operator" / "of1_5" / "OPERATOR_FLOW_TOPOLOGY_v1.0.md"
    ).read_text(encoding="utf-8")
    for edge in (
        "QualifiedDemand → Contact",
        "Contact → Deal",
        "Deal → Closed-Won",
        "Closed-Won → CommercialOutcome",
        "CommercialOutcome → Revenue Decision",
    ):
        assert edge in topology
        assert "OPERABLE" in topology


def test_freeze_operator_action_route_set_unchanged() -> None:
    prefix = of_router.router.prefix
    posts = {
        _full_route_path(route, prefix)
        for route in of_router.router.routes
        if hasattr(route, "methods") and "POST" in route.methods
    }
    assert posts == FROZEN_OPERATOR_POST_ROUTES
    gets = {
        _full_route_path(route, prefix)
        for route in of_router.router.routes
        if hasattr(route, "methods") and "GET" in route.methods
    }
    assert gets == {"/api/v1/operator/snapshot", "/api/v1/operator/operator"}


def test_freeze_degraded_state_truthful(client: TestClient) -> None:
    of1.test_path_f_page_degrades_without_fabricated_links(client, None)
    html = _operator_html()
    assert "No pending QualifiedDemand" in html
    assert "of-degraded" in html
    assert "fabricated" not in html.lower()


def test_freeze_runtime_change_reconciliation_documented() -> None:
    baseline = _baseline_doc()
    assert "Feature Code Changes" in baseline
    assert "Runtime Changes" in baseline
    assert "Not a discrepancy" in baseline
    assert "FOUNDER_OS_OPERATOR_NAME" in baseline


def test_freeze_closed_won_does_not_emit_revenue(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operator_env: None
) -> None:
    of1.test_path_c_valid_deal_stage(client, monkeypatch, operator_env)
    mc06_5.test_freeze_a3_5_emission_false()


def test_freeze_artifacts_present() -> None:
    required = (
        "OPERATOR_FLOW_BASELINE_v1.0.md",
        "OPERATOR_FLOW_TOPOLOGY_v1.0.md",
        "OPERATOR_ACTION_AUTHORITY_CONTRACT_v1.0.md",
        "OPERATOR_MUTATION_BOUNDARY_v1.0.md",
        "OPERATOR_AUDIT_IDEMPOTENCY_CONTRACT_v1.0.md",
        "OPERATOR_DEGRADED_STATE_CONTRACT_v1.0.md",
        "OPERATOR_VALUE_CHAIN_SCOPE_v1.0.md",
        "OF1_5_KNOWN_TEST_EXCEPTIONS.md",
        "OF1_5_BASELINE_MANIFEST.md",
    )
    base = ROOT / "docs" / "operator" / "of1_5"
    for name in required:
        assert (base / name).is_file(), name
