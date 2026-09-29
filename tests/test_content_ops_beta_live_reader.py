"""Live Content Ops reader: no mock fallback, server truth, server actions."""

from __future__ import annotations

import re

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import runner_api_routers.ui as ui_mod
from revenue_os.services.content_ops_authority import is_content_ops_html_path
from src.tools.content_ops_current_week import read_current_week
from src.ui.content_ops_beta.contract import TARGET_READ_CONTRACT, empty_projection
from src.ui.content_ops_beta.live_reader import (
    LiveContentOpsReadAdapter,
    envelope_for_status,
)
from src.ui.content_ops_beta.presenter import present_content_ops_read
from src.ui.content_ops_beta.reader import (
    FIXTURE_ENV,
    get_content_ops_reader,
    set_content_ops_reader,
)

_TRUTH_TOKENS = {
    "unproven",
    "remote_write_confirmed",
    "verification_pending",
    "verified",
    "failed",
    "unknown_remote",
}


@pytest.fixture
def client(cms_client: TestClient) -> TestClient:
    return cms_client


@pytest.fixture(autouse=True)
def _reset_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FIXTURE_ENV, raising=False)
    set_content_ops_reader(None)
    yield
    set_content_ops_reader(None)


def _ready(truth: str, verification: str, **fields: object) -> dict:
    projection = empty_projection()
    projection.update(
        {
            "week_id": "W12",
            "stage": "publication",
            "next_action": "Server next action",
            "publication_truth": truth,
            "verification_status": verification,
            "publication_status": "not_started",
            "allowed_actions": ["view_audit"],
        }
    )
    projection.update(fields)
    return {
        "ok": True,
        "source": "content_ops_publication_truth",
        "live": True,
        "contract": TARGET_READ_CONTRACT,
        "state": "ready",
        "http_status": 200,
        "reason": "current_week",
        "projection": projection,
        "error": None,
        "available_scenarios": [],
    }


class _Stub:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def read_current_week(self, scenario: str | None = None) -> dict:
        del scenario
        return self.payload


def _page(client: TestClient, path: str = "/content-ops") -> tuple[int, str]:
    response = client.get(path)
    return response.status_code, response.text


def test_default_reader_is_live_and_fixture_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(get_content_ops_reader(), LiveContentOpsReadAdapter)
    monkeypatch.setenv(FIXTURE_ENV, "1")
    reader = get_content_ops_reader()
    assert reader.__class__.__name__ == "MockContentOpsReadAdapter"
    payload = reader.read_current_week("verified")
    assert payload["source"] == "mock"
    assert payload["live"] is False


def test_live_read_uses_the_current_week_projection() -> None:
    payload = LiveContentOpsReadAdapter().read_current_week("verified")
    direct = read_current_week()
    assert payload["source"] == "content_ops_publication_truth"
    assert payload["live"] is True
    assert payload["contract"] == TARGET_READ_CONTRACT
    assert payload["available_scenarios"] == []
    assert payload["scenario"] != "verified"
    assert payload["projection"]["week_id"] == direct["projection"]["week_id"]
    truth = payload["projection"]["publication_truth"]
    assert truth in _TRUTH_TOKENS


def test_api_failure_does_not_fall_back_to_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(root: object = None) -> dict:
        del root
        raise RuntimeError("SECRET_STACK token fixture-week")

    monkeypatch.setattr(
        "src.ui.content_ops_beta.live_reader.read_current_week",
        _boom,
    )
    payload = LiveContentOpsReadAdapter().read_current_week("verified")
    assert payload["ok"] is False
    assert payload["source"] != "mock"
    assert payload["http_status"] == 500
    assert "SECRET_STACK" not in str(payload)
    assert payload["projection"] is None


def test_content_ops_page_is_on_the_tenant_path() -> None:
    assert is_content_ops_html_path("/content-ops") is True
    assert is_content_ops_html_path("/content-ops?scenario=verified") is True


def test_live_page_ignores_scenario_query(client: TestClient) -> None:
    status, html = _page(client, "/content-ops?scenario=verified")
    assert status == 200
    assert 'data-source="content_ops_publication_truth"' in html
    assert 'data-live="true"' in html
    assert 'data-scenario=""' in html or "data-scenario=\"\"" in html
    assert 'data-testid="scenario-switcher"' not in html
    assert "not live Founder OS publication truth" not in html
    assert "example.invalid" not in html


@pytest.mark.parametrize(
    ("truth", "verification", "headline", "success"),
    [
        ("unproven", "unproven", "Publication unverified", False),
        ("remote_write_confirmed", "verification_pending", "Verification pending", False),
        ("verification_pending", "verification_pending", "Verification pending", False),
        ("verified", "verified", "Published &amp; verified", True),
        ("failed", "failed", "Publication failed", False),
        ("unknown_remote", "unknown_remote", "Remote status unknown", False),
    ],
)
def test_page_renders_backend_truth_tokens(
    client: TestClient,
    truth: str,
    verification: str,
    headline: str,
    success: bool,
) -> None:
    actions = ["view_audit"]
    url = None
    if truth == "verified":
        actions = ["open_published_url", "view_audit"]
        url = "https://example.invalid/verified"
    elif truth in {"unproven", "remote_write_confirmed", "unknown_remote"}:
        url = "https://example.invalid/not-proof"
    set_content_ops_reader(
        _Stub(
            _ready(
                truth,
                verification,
                published_url=url,
                allowed_actions=actions,
                failure="Remote publication result is unknown." if truth == "unknown_remote" else None,
            )
        )
    )
    status, html = _page(client)
    assert status == 200
    assert f'data-publication-truth="{truth}"' in html
    assert f'data-verification-status="{verification}"' in html
    assert headline in html
    if success:
        assert 'data-testid="open-published-success"' in html
        assert 'rel="noopener noreferrer"' in html
    else:
        assert 'data-testid="open-published-success"' not in html
        assert "pill green" not in re.search(
            r'data-testid="publication-panel"[\s\S]*?</section>',
            html,
        ).group(0)
    if truth == "unknown_remote":
        assert "Publication failed" not in html
        assert 'data-testid="unknown-remote-banner"' in html
    if truth == "remote_write_confirmed":
        assert "Published &amp; verified" not in html


def test_open_published_url_requires_server_action(client: TestClient) -> None:
    set_content_ops_reader(
        _Stub(
            _ready(
                "verified",
                "verified",
                published_url="https://example.invalid/verified",
                allowed_actions=["view_audit"],
            )
        )
    )
    _status, html = _page(client)
    assert "Published &amp; verified" in html
    assert 'data-testid="open-published-success"' not in html
    assert 'data-testid="url-not-proof"' in html


def test_server_actions_render_and_unsupported_actions_do_not(client: TestClient) -> None:
    set_content_ops_reader(
        _Stub(
            _ready(
                "unproven",
                "unproven",
                stage="human_review",
                approval_required=True,
                publication_job_id="job-1",
                allowed_actions=[
                    "approve",
                    "reject",
                    "request_changes",
                    "retry",
                    "pause_week",
                    "resume",
                    "cancel_run",
                    "retry_failed_stage",
                ],
            )
        )
    )
    _status, html = _page(client)
    ids = set(re.findall(r'data-action-id="([^"]+)"', html))
    assert {"approve", "reject", "request_changes", "retry"} <= ids
    assert "pause_week" not in ids
    assert "resume" not in ids
    assert "cancel_run" not in ids
    assert "retry_failed_stage" not in ids
    assert "Unrecognized allowed actions" in html


def test_retry_without_job_id_is_omitted(client: TestClient) -> None:
    set_content_ops_reader(
        _Stub(_ready("failed", "failed", allowed_actions=["retry", "cancel", "view_audit"]))
    )
    _status, html = _page(client)
    ids = set(re.findall(r'data-action-id="([^"]+)"', html))
    assert "retry" not in ids
    assert "cancel" not in ids
    assert "view_audit" in ids


@pytest.mark.parametrize(
    ("status", "code", "title"),
    [
        (401, "UNAUTHORIZED", "Sign-in required"),
        (403, "FORBIDDEN", "Content Ops access denied"),
        (409, "ACTIVE_INTENT", "Active intent conflict"),
        (422, "POLICY_FAILURE", "Policy failure"),
        (500, "SERVER_ERROR", "Content Ops unavailable"),
    ],
)
def test_http_errors_are_not_empty_success(
    client: TestClient, status: int, code: str, title: str
) -> None:
    set_content_ops_reader(_Stub(envelope_for_status(status)))
    page_status, html = _page(client)
    assert page_status == status
    assert f'data-error-code="{code}"' in html
    assert title in html
    assert 'data-testid="content-ops-empty"' not in html
    assert 'data-testid="publication-panel"' not in html
    assert "Traceback" not in html
    assert "SECRET" not in html


def test_login_denial_renders_access_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _denied(request: object) -> None:
        del request
        raise HTTPException(status_code=403, detail="organization override secret")

    monkeypatch.setattr(ui_mod, "founder_login_redirect", _denied)
    page_status, html = _page(client)
    assert page_status == 403
    assert "Content Ops access denied" in html
    assert "organization override secret" not in html
    assert 'data-testid="content-ops-empty"' not in html


def test_empty_and_loading_states(client: TestClient) -> None:
    set_content_ops_reader(
        _Stub(
            {
                "ok": True,
                "source": "content_ops_publication_truth",
                "live": True,
                "contract": TARGET_READ_CONTRACT,
                "state": "empty",
                "http_status": 200,
                "reason": "malformed_current_week",
                "projection": empty_projection(),
                "error": None,
                "available_scenarios": [],
            }
        )
    )
    status, html = _page(client)
    assert status == 200
    assert 'data-empty-reason="malformed_current_week"' in html
    assert "Current week setting is unusable" in html
    assert 'data-testid="publication-panel"' not in html

    set_content_ops_reader(
        _Stub(
            {
                "ok": True,
                "source": "content_ops_publication_truth",
                "live": True,
                "state": "loading",
                "http_status": 200,
                "projection": None,
                "error": None,
                "available_scenarios": [],
            }
        )
    )
    status, html = _page(client)
    assert status == 200
    assert 'data-read-state="loading"' in html
    assert 'aria-busy="true"' in html


def test_nav_group_stays_open_inside_content_ops(client: TestClient) -> None:
    _status, html = _page(client)
    assert 'data-testid="content-ops-nav"' in html
    nav = re.search(r'<details\b[^>]*data-testid="content-ops-nav"[^>]*>', html)
    assert nav and 'data-persistent="true"' in nav.group(0)
    assert 'href="/content-ops"' in html
    assert 'href="/weeks"' in html
    assert 'href="/editorial"' in html
    weeks = client.get("/weeks")
    assert weeks.status_code == 200
    weeks_nav = re.search(
        r'<details\b[^>]*data-testid="content-ops-nav"[^>]*>',
        weeks.text,
    )
    assert weeks_nav and 'data-persistent="true"' in weeks_nav.group(0)
    login = client.get("/login")
    login_nav = re.search(
        r'<details\b[^>]*data-testid="content-ops-nav"[^>]*>',
        login.text,
    )
    assert login_nav and 'data-persistent="true"' not in login_nav.group(0)


def test_presenter_keeps_truth_tokens_distinct() -> None:
    remote = present_content_ops_read(
        _ready(
            "remote_write_confirmed",
            "verification_pending",
            published_url="https://example.invalid/remote",
            allowed_actions=["view_audit"],
        )
    )
    unknown = present_content_ops_read(
        _ready(
            "unknown_remote",
            "unknown_remote",
            failure="Remote publication result is unknown.",
            allowed_actions=["view_audit"],
        )
    )
    failed = present_content_ops_read(_ready("failed", "failed", failure="Publication failed."))
    assert remote["publication"]["tone"] != "verified"
    assert remote["publication"]["show_success_link"] is False
    assert unknown["publication"]["headline"] == "Remote status unknown"
    assert unknown["publication"]["headline"] != failed["publication"]["headline"]
    assert unknown["run"]["failure"]["kind"] == "unknown"
    assert failed["run"]["failure"]["kind"] == "failed"
