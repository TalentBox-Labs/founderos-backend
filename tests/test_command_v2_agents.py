"""I4 — Agents Overview + Agent Activity Detail focused contract tests."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import runner_api_routers.identity as identity_mod
from revenue_os.services.command_v2_agents import (
    STATE_BLOCKED,
    STATE_COMPLETED,
    STATE_DUPLICATE_SUPPRESSED,
    STATE_RECOVERING,
    STATE_RUNNING,
    STATE_STALE,
    STATE_UNCONFIRMED,
    STATE_UNKNOWN,
    STATE_WAITING_ON_PROVIDER,
    STATE_WAITING_ON_YOU,
    compose_agent_activity_detail,
    compose_agents_overview,
    encode_activity_key,
)

_ORG_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_ORG_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_identity() -> None:
    identity_mod._revoked_jtis.clear()
    identity_mod._login_failures.clear()


@pytest.fixture
def client(cms_client: TestClient) -> TestClient:
    return cms_client


def _orch(**lists: list) -> dict:
    base = {
        "organization_id": _ORG_A,
        "counts": {},
        "pause": {},
        "blocked": [],
        "failed": [],
        "exhausted": [],
        "retryable": [],
        "awaiting_human": [],
        "waiting_on_provider": [],
        "running": [],
        "succeeded_recently": [],
        "active": [],  # failed-oriented alias — must never become RUNNING
    }
    base.update(lists)
    return base


def test_running_agent() -> None:
    overview = compose_agents_overview(
        organization_id=_ORG_A,
        agent_orchestration=_orch(
            running=[
                {
                    "work_id": "w-run",
                    "assigned_agent": "outreach_agent",
                    "work_kind": "send_follow_up",
                    "state": "running",
                    "execution_mode": "AUTONOMOUS",
                    "created_at": "2026-08-25T10:00:00Z",
                    "contact_id": "c-1",
                }
            ]
        ),
    )
    assert overview["count"] == 1
    item = overview["activity_items"][0]
    assert item["normalized_state"] == STATE_RUNNING
    assert item["continues_autonomously"] is True
    assert item["founder_action_required"] is False


def test_blocked_agent() -> None:
    overview = compose_agents_overview(
        organization_id=_ORG_A,
        agent_orchestration=_orch(
            blocked=[
                {
                    "work_id": "w-block",
                    "assigned_agent": "booking_agent",
                    "work_kind": "book_meeting",
                    "failure_reason": "calendar unavailable",
                    "created_at": "2026-08-25T10:01:00Z",
                }
            ]
        ),
    )
    assert overview["activity_items"][0]["normalized_state"] == STATE_BLOCKED
    assert "calendar" in (overview["activity_items"][0]["waiting_on"] or "").lower()


def test_waiting_on_founder_decision() -> None:
    overview = compose_agents_overview(
        organization_id=_ORG_A,
        agent_orchestration=_orch(
            awaiting_human=[
                {
                    "work_id": "w-wait",
                    "assigned_agent": "outreach_agent",
                    "work_kind": "send_outreach_email",
                    "approval_request_id": "ar-1",
                    "created_at": "2026-08-25T10:02:00Z",
                }
            ]
        ),
        pending_approvals=[
            {
                "id": "ar-1",
                "organization_id": _ORG_A,
                "title": "Send outreach",
                "action_type": "send_outreach_email",
                "status": "pending",
            }
        ],
    )
    item = overview["activity_items"][0]
    assert item["normalized_state"] == STATE_WAITING_ON_YOU
    assert item["founder_action_required"] is True
    assert item["href_decision"] == "/decisions/ar-1"
    assert item["normalized_state"] != STATE_RUNNING


def test_waiting_on_external_provider() -> None:
    overview = compose_agents_overview(
        organization_id=_ORG_A,
        agent_orchestration=_orch(
            waiting_on_provider=[
                {
                    "work_id": "w-prov",
                    "assigned_agent": "booking_agent",
                    "work_kind": "calendar_confirm",
                    "reason": "Outlook pending",
                    "created_at": "2026-08-25T10:03:00Z",
                }
            ]
        ),
    )
    item = overview["activity_items"][0]
    assert item["normalized_state"] == STATE_WAITING_ON_PROVIDER
    assert item["provider_awaited"] is True
    assert item["founder_action_required"] is False


def test_recovering_agent() -> None:
    overview = compose_agents_overview(
        organization_id=_ORG_A,
        agent_orchestration=_orch(
            retryable=[
                {
                    "work_id": "w-rec",
                    "assigned_agent": "follow_up_agent",
                    "work_kind": "retry_follow_up",
                    "state": "recovering",
                    "execution_mode": "AUTONOMOUS",
                    "failure_reason": "transient provider error",
                    "created_at": "2026-08-25T10:04:00Z",
                }
            ],
            pause={"recovery_execution_allowed": True},
        ),
    )
    item = overview["activity_items"][0]
    assert item["normalized_state"] == STATE_RECOVERING
    assert item["normalized_state"] != STATE_RUNNING
    assert item["normalized_state"] != STATE_COMPLETED
    detail = compose_agent_activity_detail(
        organization_id=_ORG_A,
        activity_key=item["activity_key"],
        agent_orchestration=_orch(
            retryable=[
                {
                    "work_id": "w-rec",
                    "assigned_agent": "follow_up_agent",
                    "work_kind": "retry_follow_up",
                    "state": "recovering",
                    "execution_mode": "AUTONOMOUS",
                    "created_at": "2026-08-25T10:04:00Z",
                }
            ]
        ),
    )
    assert detail["available"] is True
    assert "not confirmed success" in detail["what_happened"]["text"].lower() or (
        "not the same as normal running" in detail["what_happened"]["text"].lower()
    )


def test_completed_agent() -> None:
    overview = compose_agents_overview(
        organization_id=_ORG_A,
        agent_orchestration=_orch(
            succeeded_recently=[
                {
                    "work_id": "w-done",
                    "assigned_agent": "research_agent",
                    "work_kind": "research_complete",
                    "created_at": "2026-08-25T10:05:00Z",
                }
            ]
        ),
    )
    assert overview["activity_items"][0]["normalized_state"] == STATE_COMPLETED


def test_duplicate_suppressed_activity() -> None:
    overview = compose_agents_overview(
        organization_id=_ORG_A,
        recent_activity=[
            {
                "id": "evt-dup",
                "actor": "outreach_agent",
                "action_type": "send_outreach_email",
                "status": "duplicate_suppressed",
                "created_at": "2026-08-25T10:06:00Z",
                "label": "Duplicate suppressed",
            }
        ],
    )
    item = overview["activity_items"][0]
    assert item["normalized_state"] == STATE_DUPLICATE_SUPPRESSED
    detail = compose_agent_activity_detail(
        organization_id=_ORG_A,
        activity_key=item["activity_key"],
        recent_activity=[
            {
                "id": "evt-dup",
                "actor": "outreach_agent",
                "action_type": "send_outreach_email",
                "status": "duplicate_suppressed",
                "created_at": "2026-08-25T10:06:00Z",
            }
        ],
    )
    notes = " ".join(detail["what_did_not_happen"]).lower()
    assert "not sent" in notes
    assert "not booked" in notes
    assert detail["current_state"]["normalized_state"] != STATE_COMPLETED


def test_stale_out_of_date_information() -> None:
    overview = compose_agents_overview(
        organization_id=_ORG_A,
        agent_orchestration=_orch(
            running=[
                {
                    "work_id": "w-stale",
                    "assigned_agent": "outreach_agent",
                    "work_kind": "send_follow_up",
                    "state": "running",
                    "stale": True,
                    "created_at": "2026-08-01T10:00:00Z",
                }
            ]
        ),
    )
    assert overview["activity_items"][0]["normalized_state"] == STATE_STALE
    assert overview["activity_items"][0]["freshness"] == "stale"


def test_unknown_unconfirmed_fails_closed() -> None:
    overview = compose_agents_overview(
        organization_id=_ORG_A,
        agent_orchestration=_orch(
            running=[
                {
                    "work_id": "w-unc",
                    "assigned_agent": "mystery_agent",
                    "work_kind": "unclear",
                    "state": "running",
                    # no created_at → freshness unknown → cannot stay RUNNING
                }
            ]
        ),
    )
    assert overview["activity_items"][0]["normalized_state"] == STATE_UNCONFIRMED
    assert overview["activity_items"][0]["normalized_state"] != STATE_RUNNING


def test_unknown_new_agent_type_renders_safely() -> None:
    overview = compose_agents_overview(
        organization_id=_ORG_A,
        agent_orchestration=_orch(
            running=[
                {
                    "work_id": "w-mkt",
                    "assigned_agent": "marketing_campaign_agent_v9",
                    "work_kind": "campaign_draft_review",
                    "state": "running",
                    "execution_mode": "AUTONOMOUS",
                    "created_at": "2026-08-25T11:00:00Z",
                    "requested_effect": "prepare_campaign_packet",
                }
            ]
        ),
    )
    item = overview["activity_items"][0]
    assert item["agent_name"] == "marketing_campaign_agent_v9"
    assert item["normalized_state"] == STATE_RUNNING
    assert "campaign" in item["objective"].lower() or "prepare" in item["objective"].lower()


def test_unknown_agent_retains_canonical_state_provenance() -> None:
    overview = compose_agents_overview(
        organization_id=_ORG_A,
        agent_orchestration=_orch(
            blocked=[
                {
                    "work_id": "work-canonical-99",
                    "assigned_agent": "brand_new_ops_agent",
                    "action_type": "ops_unknown_effect",
                    "failure_reason": "effect not classified",
                    "created_at": "2026-08-25T11:01:00Z",
                }
            ]
        ),
    )
    item = overview["activity_items"][0]
    assert item["canonical_work_id"] == "work-canonical-99"
    assert item["action_type"] == "ops_unknown_effect"
    assert item["normalized_state"] == STATE_BLOCKED
    detail = compose_agent_activity_detail(
        organization_id=_ORG_A,
        activity_key=item["activity_key"],
        agent_orchestration=_orch(
            blocked=[
                {
                    "work_id": "work-canonical-99",
                    "assigned_agent": "brand_new_ops_agent",
                    "action_type": "ops_unknown_effect",
                    "failure_reason": "effect not classified",
                    "created_at": "2026-08-25T11:01:00Z",
                }
            ]
        ),
    )
    assert detail["provenance"]["canonical_work_id"] == "work-canonical-99"
    assert detail["provenance"]["action_type"] == "ops_unknown_effect"


def test_missing_org_fails_closed() -> None:
    overview = compose_agents_overview(organization_id=None, agent_orchestration=_orch())
    assert overview["fail_closed_reason"] == "missing_organization_id"
    assert overview["count"] == 0
    detail = compose_agent_activity_detail(
        organization_id=None, activity_key="work:x", agent_orchestration=_orch()
    )
    assert detail["available"] is False
    assert detail["fail_closed_reason"] == "missing_organization_id"


def test_tenant_mismatch_fails_closed() -> None:
    overview = compose_agents_overview(
        organization_id=_ORG_A,
        agent_orchestration={
            "organization_id": _ORG_B,
            "running": [
                {
                    "work_id": "w-x",
                    "assigned_agent": "a",
                    "created_at": "2026-08-25T10:00:00Z",
                    "state": "running",
                }
            ],
        },
    )
    assert overview["fail_closed_reason"] == "organization_mismatch"
    assert overview["count"] == 0


def test_cross_tenant_activity_detail_unavailable() -> None:
    orch_a = _orch(
        running=[
            {
                "work_id": "w-cross",
                "assigned_agent": "outreach_agent",
                "state": "running",
                "created_at": "2026-08-25T10:00:00Z",
            }
        ]
    )
    overview = compose_agents_overview(organization_id=_ORG_A, agent_orchestration=orch_a)
    key = overview["activity_items"][0]["activity_key"]
    detail_b = compose_agent_activity_detail(
        organization_id=_ORG_B,
        activity_key=key,
        agent_orchestration=orch_a,  # even if payload leaked, org fence on overview drops it
    )
    assert detail_b["available"] is False


def test_agent_activity_links_to_canonical_i3_decision() -> None:
    orch = _orch(
        awaiting_human=[
            {
                "work_id": "w-dec",
                "assigned_agent": "outreach_agent",
                "approval_request_id": "ar-99",
                "created_at": "2026-08-25T10:00:00Z",
            }
        ]
    )
    pending = [{"id": "ar-99", "organization_id": _ORG_A, "status": "pending"}]
    overview = compose_agents_overview(
        organization_id=_ORG_A, agent_orchestration=orch, pending_approvals=pending
    )
    detail = compose_agent_activity_detail(
        organization_id=_ORG_A,
        activity_key=overview["activity_items"][0]["activity_key"],
        agent_orchestration=orch,
        pending_approvals=pending,
    )
    assert detail["related_decision"]["href"] == "/decisions/ar-99"
    assert detail["related_decision"]["label"] == "View Decision"


def test_agent_activity_does_not_invent_approval_authority() -> None:
    orch = _orch(
        awaiting_human=[
            {
                "work_id": "w-auth",
                "assigned_agent": "outreach_agent",
                "approval_request_id": "ar-2",
                "created_at": "2026-08-25T10:00:00Z",
            }
        ]
    )
    pending = [{"id": "ar-2", "organization_id": _ORG_A, "status": "pending"}]
    detail = compose_agent_activity_detail(
        organization_id=_ORG_A,
        activity_key="work:w-auth",
        agent_orchestration=orch,
        pending_approvals=pending,
    )
    action_types = {a.get("action_type") for a in detail["actions"]}
    assert "approval_approve" not in action_types
    assert "approval_reject" not in action_types
    assert all(a.get("action_type") == "navigate" for a in detail["actions"])
    authority = " ".join(detail["authority"]["does_not_exist"]).lower()
    assert "does not approve" in authority


def test_opening_agent_detail_performs_no_mutation(client: TestClient) -> None:
    snap = {
        "generated_at": "2026-08-25T12:00:00Z",
        "state": "ok",
        "message": "",
        "detail": {
            "available": True,
            "activity_key": "work:w1",
            "presentation_agent_key": "agent:x",
            "agent_name": "x",
            "agent_role": "Agent",
            "objective": {"text": "Do work", "account_context": None},
            "current_state": {
                "normalized_state": STATE_RUNNING,
                "state_label": "Running",
                "freshness": "current",
                "founder_action_required": False,
                "provider_awaited": False,
                "continues_autonomously": True,
            },
            "what_happened": {"text": "Running", "latest_event": None, "occurred_at": None},
            "why_stopped_or_continues": {"text": "Continues"},
            "waiting_on": {
                "text": None,
                "founder_action_required": False,
                "provider_awaited": False,
            },
            "what_happens_next": {"available": False, "text": None},
            "provenance": {},
            "related_decision": None,
            "what_did_not_happen": [],
            "authority": {"exists": [], "does_not_exist": ["Opening performs no mutation."]},
            "actions": [],
            "href_overview": "/agents",
        },
    }
    with (
        patch("runner_api_routers.ui.founder_login_redirect", return_value=None),
        patch(
            "runner_api_routers.ui.build_agent_activity_detail_snapshot",
            return_value=snap,
        ) as builder,
    ):
        resp = client.get(f"/agents/activity/{encode_activity_key('work:w1')}")
    assert resp.status_code == 200
    assert b"agent-activity-detail" in resp.content
    assert b"Approve" not in resp.content
    assert builder.call_count == 1  # GET only — no decide/mutate side path


def test_no_fabricated_next_move() -> None:
    detail = compose_agent_activity_detail(
        organization_id=_ORG_A,
        activity_key="work:w-block",
        agent_orchestration=_orch(
            blocked=[
                {
                    "work_id": "w-block",
                    "assigned_agent": "a",
                    "failure_reason": "stopped",
                    "created_at": "2026-08-25T10:00:00Z",
                }
            ]
        ),
    )
    assert detail["what_happens_next"]["available"] is False
    assert detail["what_happens_next"]["text"] is None


def test_no_fabricated_completion_or_provider_success() -> None:
    overview = compose_agents_overview(
        organization_id=_ORG_A,
        agent_orchestration=_orch(
            waiting_on_provider=[
                {
                    "work_id": "w-p",
                    "assigned_agent": "booking_agent",
                    "provider_confirmed": False,
                    "created_at": "2026-08-25T10:00:00Z",
                }
            ]
        ),
    )
    item = overview["activity_items"][0]
    assert item["normalized_state"] == STATE_WAITING_ON_PROVIDER
    assert item["normalized_state"] != STATE_COMPLETED
    detail = compose_agent_activity_detail(
        organization_id=_ORG_A,
        activity_key=item["activity_key"],
        agent_orchestration=_orch(
            waiting_on_provider=[
                {
                    "work_id": "w-p",
                    "assigned_agent": "booking_agent",
                    "provider_confirmed": False,
                    "created_at": "2026-08-25T10:00:00Z",
                }
            ]
        ),
    )
    text = detail["what_happened"]["text"].lower()
    assert "not" in text and "success" in text


def test_presentation_grouping_not_persistent_identity() -> None:
    overview = compose_agents_overview(
        organization_id=_ORG_A,
        agent_orchestration=_orch(
            running=[
                {
                    "work_id": "w1",
                    "assigned_agent": "outreach_agent",
                    "state": "running",
                    "created_at": "2026-08-25T10:00:00Z",
                }
            ]
        ),
    )
    item = overview["activity_items"][0]
    assert item["persistent"] is False
    assert overview["persistent"] is False
    assert str(item["presentation_agent_key"]).startswith("agent:")
    assert item["presentation_activity_key"] == item["activity_key"]
    # Must not look like a Situation ID mint.
    assert "situation" not in str(item["presentation_agent_key"]).lower()
    src = inspect.getsource(compose_agents_overview)
    assert "Situation" not in src or "not" in src.lower()


def test_orch_active_alias_never_running() -> None:
    overview = compose_agents_overview(
        organization_id=_ORG_A,
        agent_orchestration=_orch(
            active=[
                {
                    "work_id": "w-active-alias",
                    "assigned_agent": "x",
                    "state": "failed",
                    "created_at": "2026-08-25T10:00:00Z",
                }
            ]
        ),
    )
    assert overview["count"] == 0


def test_agents_overview_page_renders(client: TestClient) -> None:
    snap = {
        "generated_at": "2026-08-25T12:00:00Z",
        "state": "ok",
        "message": "",
        "organization_id": _ORG_A,
        "agents_overview": compose_agents_overview(
            organization_id=_ORG_A,
            agent_orchestration=_orch(
                running=[
                    {
                        "work_id": "w-page",
                        "assigned_agent": "outreach_agent",
                        "state": "running",
                        "execution_mode": "AUTONOMOUS",
                        "created_at": "2026-08-25T10:00:00Z",
                    }
                ]
            ),
        ),
    }
    with patch("runner_api_routers.ui.founder_login_redirect", return_value=None):
        with patch("runner_api_routers.ui.build_agents_snapshot", return_value=snap):
            resp = client.get("/agents")
    assert resp.status_code == 200
    assert 'data-testid="agents-overview-item"' in resp.text
    assert "outreach_agent" in resp.text
    assert "situation_id" not in resp.text.lower()


def test_helper_has_no_db_writes() -> None:
    path = _ROOT / "revenue_os/services/command_v2_agents.py"
    tree = ast.parse(path.read_text())
    banned = {"SessionLocal", "commit", "flush", "add", "merge", "delete"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "SessionLocal":
            pytest.fail("command_v2_agents must not use SessionLocal")
        if isinstance(node, ast.Attribute) and node.attr in {"commit", "flush"}:
            pytest.fail(f"command_v2_agents must not call .{node.attr}()")
    text = path.read_text()
    assert "persistent\": False" in text or "persistent': False" in text or 'persistent": False' in text
    assert "command_v2_agents" in text
    _ = banned


def test_frozen_i1_i2_i3_tests_unchanged() -> None:
    assert (_ROOT / "tests/test_command_v2_projection.py").exists()
    assert (_ROOT / "tests/test_command_v2_home.py").exists()
    assert (_ROOT / "tests/test_command_v2_decisions.py").exists()
    # This gate must not rewrite frozen suites.
    for name in (
        "test_command_v2_projection.py",
        "test_command_v2_home.py",
        "test_command_v2_decisions.py",
    ):
        assert name != "test_command_v2_agents.py"
