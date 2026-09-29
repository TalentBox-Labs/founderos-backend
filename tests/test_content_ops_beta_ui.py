"""Content Ops Beta UI — mock contract, truth rendering, capabilities, navigation."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import runner_api_routers.ui as ui_mod
from src.ui.content_ops_beta.contract import PROJECTION_FIELDS, TARGET_READ_CONTRACT, empty_projection
from src.ui.content_ops_beta.mock_adapter import SCENARIO_CATALOG, MockContentOpsReadAdapter
from src.ui.content_ops_beta.presenter import present_content_ops_read
from src.ui.content_ops_beta.reader import FIXTURE_ENV, set_content_ops_reader

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "content_ops_beta.html"
ADAPTER = ROOT / "src" / "ui" / "content_ops_beta" / "mock_adapter.py"

MANDATORY_SCENARIOS = (
    "empty",
    "research_drafting",
    "qa_pass",
    "human_review",
    "approved_publish_pending",
    "publication_unproven",
    "verification_pending",
    "verified",
    "failed",
    "unknown_remote",
    "http_401",
    "http_403",
    "http_409",
    "http_422",
)


@pytest.fixture
def client(cms_client: TestClient) -> TestClient:
    return cms_client


@pytest.fixture(autouse=True)
def _reset_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FIXTURE_ENV, "1")
    set_content_ops_reader(None)
    yield
    set_content_ops_reader(None)


def _page(client: TestClient, scenario: str | None = None) -> str:
    path = "/content-ops" if scenario is None else f"/content-ops?scenario={scenario}"
    response = client.get(path)
    if scenario and scenario.startswith("http_"):
        expected = int(scenario.split("_", 1)[1])
        assert response.status_code == expected, response.text
    else:
        assert response.status_code == 200, response.text
    return response.text


def _action_ids(html: str) -> set[str]:
    return set(re.findall(r'data-action-id="([^"]+)"', html))


def _panel(html: str, testid: str) -> str:
    match = re.search(
        rf'(<section\b[^>]*data-testid="{testid}"[\s\S]*?</section>)',
        html,
    )
    assert match, testid
    return match.group(1)


def _nav_tag(html: str) -> str:
    match = re.search(r'<details\b[^>]*data-testid="content-ops-nav"[^>]*>', html)
    assert match, html[html.find("Content Ops") : html.find("Content Ops") + 200]
    return match.group(0)


def _nav_open(html: str) -> bool:
    return bool(re.search(r"\sopen(?:\s|>)", _nav_tag(html)))


def test_mock_adapter_is_isolated_and_not_live() -> None:
    source = ADAPTER.read_text(encoding="utf-8")
    lowered = source.lower()
    assert "tracker.csv" not in lowered
    assert "_read_tracker" not in lowered
    assert "frontmatter" not in lowered
    assert "hashnode" not in lowered
    adapter = MockContentOpsReadAdapter()
    for scenario in MANDATORY_SCENARIOS:
        payload = adapter.read_current_week(scenario)
        assert payload["source"] == "mock"
        assert payload["live"] is False
        assert payload["contract"] == TARGET_READ_CONTRACT
        assert payload["scenario"] == scenario
        if payload["ok"]:
            assert set(PROJECTION_FIELDS).issubset(payload["projection"])
        else:
            assert payload["projection"] is None
            assert payload["error"]["code"]
            assert payload["error"]["message"]


def test_unknown_scenario_does_not_invent_success() -> None:
    payload = MockContentOpsReadAdapter().read_current_week("published_ok")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "UNKNOWN_SCENARIO"
    view = present_content_ops_read(payload)
    assert view["read_state"] == "error"
    assert view["live"] is False


def test_mock_live_flag_cannot_mark_itself_live() -> None:
    raw = MockContentOpsReadAdapter().read_current_week("verified")
    raw["live"] = True
    view = present_content_ops_read(raw)
    assert view["live"] is False
    assert view["mock_banner"] is True


def test_default_page_is_empty_mock(client: TestClient) -> None:
    html = _page(client)
    assert 'data-live="false"' in html
    assert 'data-source="mock"' in html
    assert 'data-read-state="empty"' in html
    assert 'data-testid="content-ops-empty"' in html
    assert "No active week" in html
    assert "not live Founder OS publication truth" in html
    assert 'data-testid="open-published-success"' not in html
    assert "PUBLISHED / VERIFIED" not in html
    assert _action_ids(html) == set()


@pytest.mark.parametrize(
    ("scenario", "headline"),
    [
        ("research_drafting", "Publication unverified"),
        ("qa_pass", "Publication unverified"),
        ("human_review", "Publication unverified"),
        ("approved_publish_pending", "Publication unverified"),
        ("publication_unproven", "Publication unverified"),
        ("verification_pending", "Verification pending"),
        ("verified", "Published &amp; verified"),
        ("unknown_remote", "Remote status unknown"),
        ("failed", "Publication failed"),
    ],
)
def test_publication_headline_follows_contract(
    client: TestClient, scenario: str, headline: str
) -> None:
    html = _page(client, scenario)
    assert f'data-testid="publication-headline">{headline}<' in html
    panel = _panel(html, "publication-panel")
    if scenario == "verified":
        assert 'data-tone="verified"' in panel
        assert 'data-testid="open-published-success"' in panel
        assert "cop-verified-action" in panel
    else:
        assert 'data-tone="verified"' not in panel
        assert 'data-testid="open-published-success"' not in html
        assert re.search(r'class="[^"]*cop-verified-action', html) is None
        assert "Published &amp; verified" not in html


def test_unproven_url_is_not_a_success_link(client: TestClient) -> None:
    html = _page(client, "publication_unproven")
    panel = _panel(html, "publication-panel")
    assert "https://example.invalid/content/w12" in panel
    assert 'data-testid="url-not-proof"' in panel
    assert 'data-testid="open-published-withheld"' not in panel
    assert "pill green" not in panel
    assert 'data-publication-truth="unproven"' in panel
    assert 'data-action-id="open_published_url"' not in panel
    success = re.search(r'<a\b[^>]*data-testid="open-published-success"', html)
    assert success is None


def test_unverified_pending_is_distinct_from_verified(client: TestClient) -> None:
    pending = _page(client, "verification_pending")
    verified = _page(client, "verified")
    assert 'data-testid="publication-headline">Verification pending<' in pending
    assert "remote_write_confirmed" in pending
    assert "Published &amp; verified" not in pending
    assert 'data-testid="open-published-success"' not in pending
    assert 'data-testid="publication-headline">Published &amp; verified<' in verified
    assert 'data-testid="publication-headline">Verification pending<' not in verified
    assert 'data-testid="publication-unverified"' not in verified


def test_qa_pass_green_stays_on_qa_panel(client: TestClient) -> None:
    html = _page(client, "qa_pass")
    qa = _panel(html, "qa-panel")
    publication = _panel(html, "publication-panel")
    assert "pill green" in qa
    assert "QA PASS" in qa
    assert "pill green" not in publication
    assert "It is not publication." in qa


def test_failed_state_shows_blocker_without_success(client: TestClient) -> None:
    html = _page(client, "failed")
    assert 'data-testid="failure-banner"' in html
    assert "Draft stage failed. No publication was attempted." in html
    assert "retry" in _action_ids(html)
    assert "cancel" in _action_ids(html)
    assert "retry_failed_stage" not in _action_ids(html)
    assert "pause_week" not in html
    assert "cancel_run" not in html
    assert "open_published_url" not in _action_ids(html)
    assert 'data-testid="open-published-success"' not in html


def test_unknown_remote_is_not_success(client: TestClient) -> None:
    html = _page(client, "unknown_remote")
    panel = _panel(html, "publication-panel")
    assert 'data-tone="unknown"' in panel
    assert 'data-publication-truth="unknown_remote"' in panel
    assert "Publication failed" not in panel
    assert "pill green" not in panel
    assert 'data-testid="url-not-proof"' in panel
    assert 'data-testid="open-published-success"' not in panel
    assert _action_ids(html) == {"view_audit"}


@pytest.mark.parametrize(
    ("scenario", "code", "status"),
    [
        ("http_401", "UNAUTHORIZED", "401"),
        ("http_403", "FORBIDDEN", "403"),
        ("http_409", "ACTIVE_INTENT", "409"),
        ("http_422", "POLICY_FAILURE", "422"),
    ],
)
def test_error_states_load_no_week(
    client: TestClient, scenario: str, code: str, status: str
) -> None:
    html = _page(client, scenario)
    assert f'data-error-code="{code}"' in html
    assert f'data-http-status="{status}"' in html
    assert 'role="alert"' in html
    assert 'data-testid="publication-panel"' not in html
    assert 'data-testid="open-published-success"' not in html
    assert "PUBLISHED / VERIFIED" not in html
    assert _action_ids(html) == set()


def test_conflict_does_not_become_active_published_week(client: TestClient) -> None:
    html = _page(client, "http_409")
    assert "No new run was started" in html
    assert "not an active published week" in html


def test_policy_failure_states_nothing_published(client: TestClient) -> None:
    html = _page(client, "http_422")
    assert "Nothing was published" in html
    assert "human_approval_required" in html


def test_loading_state(client: TestClient) -> None:
    html = _page(client, "loading")
    assert 'data-read-state="loading"' in html
    assert 'aria-busy="true"' in html
    assert 'data-testid="content-ops-loading"' in html
    assert 'data-testid="publication-panel"' not in html


def test_human_review_capabilities_only(client: TestClient) -> None:
    html = _page(client, "human_review")
    assert _action_ids(html) == {
        "approve",
        "reject",
        "request_changes",
        "open_artifact",
        "view_audit",
    }
    assert 'data-testid="decision-required"' in html
    assert "Approval is required." in html
    assert 'data-executes="false"' in html


def test_research_capabilities_exclude_decisions(client: TestClient) -> None:
    html = _page(client, "research_drafting")
    assert _action_ids(html) == {"run_now", "open_artifact", "view_audit"}
    assert "pause_week" not in html
    assert "resume" not in html
    assert 'data-testid="decision-empty"' in html
    assert 'data-week-id="W12"' in html
    assert 'data-stage="drafting"' in html


def test_verified_pipeline_marks_only_verification(client: TestClient) -> None:
    html = _page(client, "verified")
    assert 'data-step="verification" data-step-state="verified"' in html
    assert 'data-step="research" data-step-state="idle"' in html
    assert "Earlier steps are not marked complete." in html


def test_actions_use_existing_routes_without_publish() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "fetch(" in text
    assert "credentials: 'same-origin'" in text
    assert "url: '/generate'" in text
    assert "/api/v1/editorial/" in text
    assert "/api/v1/publishing/" in text
    assert "'/publish'" not in text
    assert "go-live" not in text
    assert "hashnode" not in text
    assert "organization_id" not in text
    assert "will not be retried automatically" in text
    assert "will not send the action again automatically" in text
    assert "pause_week" not in text
    assert "resume" not in text
    assert "cancel_run" not in text
    assert "retry_failed_stage" not in text


def test_route_does_not_read_publication_authority() -> None:
    source = inspect.getsource(ui_mod.page_content_ops)
    assert "get_content_ops_reader" in source
    assert "present_content_ops_read" in source
    assert "_read_tracker" not in source
    assert "publishing_engine" not in source
    assert "founder_login_redirect" in source


def test_reader_swap_does_not_require_template_change(client: TestClient) -> None:
    class _Stub:
        def read_current_week(self, scenario: str | None = None) -> dict:
            return {
                "source": "stub",
                "live": False,
                "contract": TARGET_READ_CONTRACT,
                "scenario": "stub-empty",
                "http_status": 200,
                "ok": True,
                "state": "empty",
                "projection": empty_projection(),
                "error": None,
                "available_scenarios": [{"id": "stub-empty", "label": "Stub empty"}],
            }

    set_content_ops_reader(_Stub())
    html = _page(client)
    assert 'data-source="stub"' in html
    assert 'data-testid="content-ops-empty"' in html
    assert "Stub empty" in html


def test_presenter_refuses_false_publication_success() -> None:
    projection = empty_projection()
    projection.update(
        {
            "week_id": "W12",
            "stage": "publication",
            "publication_status": "REPORTED",
            "publication_truth": "unproven",
            "verification_status": "unproven",
            "published_url": "https://example.invalid/content/w12",
            "allowed_actions": ["open_published_url"],
        }
    )
    view = present_content_ops_read(
        {
            "source": "mock",
            "live": False,
            "contract": TARGET_READ_CONTRACT,
            "scenario": "hostile",
            "http_status": 200,
            "ok": True,
            "state": "ready",
            "projection": projection,
            "error": None,
            "available_scenarios": [],
        }
    )
    assert view["http_status"] == 200
    assert view["publication"]["show_success_link"] is False
    assert view["publication"]["withhold_success"] is True
    assert view["publication"]["headline"] == "Publication unverified"
    assert view["publication"]["tone"] != "verified"
    assert view["publication"]["pill"] != "green"


def test_presenter_requires_both_truths_and_the_action() -> None:
    def view_for(**fields: object) -> dict:
        projection = empty_projection()
        projection.update({"week_id": "W12", "stage": "verified", **fields})
        return present_content_ops_read(
            {
                "source": "future",
                "live": True,
                "contract": TARGET_READ_CONTRACT,
                "scenario": "check",
                "http_status": 200,
                "ok": True,
                "state": "ready",
                "projection": projection,
                "error": None,
                "available_scenarios": [],
            }
        )

    proven = view_for(
        publication_truth="verified",
        verification_status="verified",
        published_url="https://example.invalid/ok",
        allowed_actions=["open_published_url"],
    )
    assert proven["publication"]["show_success_link"] is True
    missing_action = view_for(
        publication_truth="verified",
        verification_status="verified",
        published_url="https://example.invalid/ok",
        allowed_actions=["view_audit"],
    )
    assert missing_action["publication"]["show_success_link"] is False
    assert missing_action["publication"]["headline"] == "Published & verified"
    pending = view_for(
        publication_truth="remote_write_confirmed",
        verification_status="verification_pending",
        published_url="https://example.invalid/ok",
        allowed_actions=["open_published_url"],
    )
    assert pending["publication"]["show_success_link"] is False
    assert pending["publication"]["headline"] == "Verification pending"
    assert pending["publication"]["tone"] != "verified"
    unsafe = view_for(
        publication_truth="verified",
        verification_status="verified",
        published_url="javascript:alert(1)",
        allowed_actions=["open_published_url"],
    )
    assert unsafe["publication"]["show_success_link"] is False
    assert unsafe["publication"]["url"] is None


def test_missing_allowed_actions_fail_closed() -> None:
    projection = empty_projection()
    projection.update({"week_id": "W12", "stage": "drafting", "allowed_actions": "approve"})
    view = present_content_ops_read(
        {
            "source": "mock",
            "live": False,
            "ok": True,
            "state": "ready",
            "http_status": 200,
            "projection": projection,
            "error": None,
        }
    )
    assert view["week_actions"] == []
    assert view["decision_actions"] == []
    assert view["has_actions"] is False


def test_content_ops_nav_stays_open_on_content_pages(client: TestClient) -> None:
    beta = _page(client, "empty")
    weeks = client.get("/weeks")
    login = client.get("/login")
    assert weeks.status_code == 200
    assert login.status_code == 200
    assert _nav_open(beta)
    assert 'data-persistent="true"' in _nav_tag(beta)
    assert 'href="/editorial"' in beta
    assert 'href="/content-studio"' in beta
    assert 'data-current-section="true"' in beta
    assert 'data-testid="nav-content-ops-beta"' in beta
    assert 'href="/content-ops"' in beta
    assert _nav_open(weeks.text)
    assert 'data-persistent="true"' in _nav_tag(weeks.text)
    assert 'href="/content-ops"' in weeks.text
    assert 'href="/editorial"' in weeks.text
    assert _nav_open(login.text) is False
    assert 'data-persistent="true"' not in _nav_tag(login.text)
    assert 'href="/command"' in login.text
    assert 'href="/demand"' in login.text
    assert 'href="/pending-approvals"' in login.text
    assert 'href="/activity"' in login.text
    assert 'href="/operator"' in login.text


def test_scenario_catalog_covers_the_mandatory_set() -> None:
    ids = [item["id"] for item in SCENARIO_CATALOG]
    for scenario in MANDATORY_SCENARIOS:
        assert scenario in ids
