"""Command Center V2 I4 — Agents Overview + Agent Activity Detail (projection only).

Composes founder-facing agent observability from existing orchestration /
AgentActionLog / ApprovalRequest truth. Does not persist, mint Situation IDs,
create agent registries, or expand authority.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, unquote

# Normalized presentation states — never collapse distinct evidence classes.
STATE_WAITING_ON_YOU = "WAITING_ON_YOU"
STATE_WAITING_ON_PROVIDER = "WAITING_ON_PROVIDER"
STATE_BLOCKED = "BLOCKED"
STATE_DEGRADED = "DEGRADED"
STATE_RUNNING = "RUNNING"
STATE_RECOVERING = "RECOVERING"
STATE_COMPLETED = "COMPLETED"
STATE_DUPLICATE_SUPPRESSED = "DUPLICATE_SUPPRESSED"
STATE_STALE = "STALE"
STATE_UNKNOWN = "UNKNOWN"
STATE_UNCONFIRMED = "UNCONFIRMED"

_STATE_LABELS: dict[str, str] = {
    STATE_WAITING_ON_YOU: "Waiting on you",
    STATE_WAITING_ON_PROVIDER: "Waiting on provider",
    STATE_BLOCKED: "Blocked",
    STATE_DEGRADED: "Degraded",
    STATE_RUNNING: "Running",
    STATE_RECOVERING: "Recovering",
    STATE_COMPLETED: "Completed",
    STATE_DUPLICATE_SUPPRESSED: "Duplicate suppressed",
    STATE_STALE: "Out of date",
    STATE_UNKNOWN: "Unknown",
    STATE_UNCONFIRMED: "Unconfirmed",
}

# Orch list "active" is failed-oriented in compose_orchestration_summary — never RUNNING.
_ORCH_FAILED_ALIAS_KEY = "active"

_SOURCE_PRIORITY: tuple[tuple[str, str], ...] = (
    ("awaiting_human", STATE_WAITING_ON_YOU),
    ("waiting_on_provider", STATE_WAITING_ON_PROVIDER),
    ("blocked", STATE_BLOCKED),
    ("exhausted", STATE_BLOCKED),
    ("failed", STATE_BLOCKED),
    ("ambiguous_effect", STATE_UNKNOWN),
    ("claim_rejected", STATE_BLOCKED),
    ("fence_rejected", STATE_BLOCKED),
    ("retryable", STATE_RECOVERING),
    ("running", STATE_RUNNING),
    ("in_progress", STATE_RUNNING),
    ("autonomous_running", STATE_RUNNING),
    ("succeeded_recently", STATE_COMPLETED),
)


def _norm(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _humanize(token: str | None, *, fallback: str = "Agent work") -> str:
    text = _norm(token)
    if not text:
        return fallback
    return text.replace("_", " ").replace("-", " ").title()


def encode_activity_key(activity_key: str) -> str:
    """URL-safe encoding for presentation-only activity keys."""
    return quote(activity_key, safe="")


def decode_activity_key(encoded: str | None) -> str | None:
    raw = _norm(encoded)
    if not raw:
        return None
    try:
        return unquote(raw)
    except Exception:  # noqa: BLE001
        return raw


def _empty_overview(*, organization_id: str | None, reason: str) -> dict[str, Any]:
    return {
        "organization_id": organization_id,
        "source": "command_v2_agents",
        "persistent": False,
        "fail_closed_reason": reason,
        "activity_items": [],
        "count": 0,
        "agents": [],
        "agent_count": 0,
    }


def _empty_detail(*, organization_id: str | None, reason: str) -> dict[str, Any]:
    return {
        "organization_id": organization_id,
        "source": "command_v2_agents",
        "persistent": False,
        "fail_closed_reason": reason,
        "available": False,
        "actions": [],
        "related_decision": None,
    }


def _identity(item: dict[str, Any], prefix: str) -> str:
    wid = _norm(item.get("work_id"))
    if wid:
        return f"work:{wid}"
    event_id = _norm(item.get("id") or item.get("event_id") or item.get("action_log_id"))
    if event_id:
        return f"event:{event_id}"
    at = _norm(item.get("action_type") or item.get("work_kind") or item.get("requested_effect"))
    created = _norm(item.get("created_at") or item.get("occurred_at"))
    agent = _norm(item.get("assigned_agent") or item.get("actor"))
    return f"{prefix}:{agent or 'agent'}:{at or 'work'}:{created or 'na'}"


def _is_duplicate_suppressed(item: dict[str, Any]) -> bool:
    status = (_norm(item.get("status")) or "").lower()
    if "duplicate" in status or status in {"suppressed", "duplicate_suppressed"}:
        return True
    detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
    if detail.get("duplicate_suppressed") or detail.get("suppressed_duplicate"):
        return True
    if (_norm(detail.get("outcome")) or "").lower() in {
        "duplicate_suppressed",
        "suppressed",
    }:
        return True
    at = (_norm(item.get("action_type")) or "").lower()
    return "duplicate_suppress" in at


def _freshness_for(item: dict[str, Any]) -> str:
    """Return current | stale | unknown — never invent currency."""
    explicit = (_norm(item.get("freshness")) or "").lower()
    if explicit in {"stale", "out_of_date", "outdated"}:
        return "stale"
    if explicit in {"current", "fresh"}:
        return "current"
    if item.get("stale") is True or item.get("out_of_date") is True:
        return "stale"
    if item.get("unconfirmed") is True or item.get("provider_confirmed") is False:
        return "unknown"
    if _norm(item.get("created_at") or item.get("occurred_at")):
        return "current"
    return "unknown"


def _agent_display_name(item: dict[str, Any]) -> str:
    return (
        _norm(item.get("assigned_agent"))
        or _norm(item.get("actor"))
        or _norm(item.get("agent_name"))
        or "Agent"
    )


def _objective_for(item: dict[str, Any]) -> str:
    return (
        _norm(item.get("objective"))
        or _norm(item.get("requested_effect"))
        or _norm(item.get("work_kind"))
        or _humanize(item.get("action_type"), fallback="Governed work")
    )


def _context_for(item: dict[str, Any]) -> str | None:
    return (
        _norm(item.get("account_label"))
        or _norm(item.get("contact_name"))
        or _norm(item.get("target_label"))
        or _norm(item.get("contact_id") or item.get("target_id"))
    )


def _approval_id_for(item: dict[str, Any], pending_by_id: dict[str, dict[str, Any]]) -> str | None:
    ar = _norm(item.get("approval_request_id"))
    if ar and ar in pending_by_id:
        return ar
    if ar:
        return ar
    # Soft link via target when orch item references the same contact as a pending AR.
    target = _norm(item.get("contact_id") or item.get("target_id"))
    if not target:
        return None
    for pending in pending_by_id.values():
        if _norm(pending.get("target_id")) == target:
            return _norm(pending.get("id") or pending.get("request_id"))
    return None


def _normalize_state(
    *,
    source_list: str | None,
    item: dict[str, Any],
    freshness: str,
) -> str:
    if _is_duplicate_suppressed(item):
        return STATE_DUPLICATE_SUPPRESSED

    explicit = (_norm(item.get("normalized_state") or item.get("visible_state")) or "").upper()
    if explicit in _STATE_LABELS:
        state = explicit
    else:
        state_token = (_norm(item.get("state")) or "").lower()
        status_token = (_norm(item.get("status")) or "").lower()
        if state_token in {"recovering", "retryable"}:
            state = STATE_RECOVERING
        elif state_token in {"running", "in_progress", "progressing", "proposed"}:
            state = STATE_RUNNING
        elif state_token in {"completed", "succeeded", "success"}:
            state = STATE_COMPLETED
        elif state_token in {"blocked", "failed", "exhausted"}:
            state = STATE_BLOCKED
        elif state_token in {"waiting_on_provider", "provider_wait"}:
            state = STATE_WAITING_ON_PROVIDER
        elif state_token in {"awaiting_human", "waiting_on_you", "pending_human"}:
            state = STATE_WAITING_ON_YOU
        elif state_token in {"degraded"}:
            state = STATE_DEGRADED
        elif state_token in {"unconfirmed"}:
            state = STATE_UNCONFIRMED
        elif status_token in {"completed", "succeeded", "success", "ok"}:
            state = STATE_COMPLETED
        elif status_token in {"failed", "error", "blocked"}:
            state = STATE_BLOCKED
        elif status_token in {"pending", "awaiting_human"}:
            state = STATE_WAITING_ON_YOU
        elif source_list:
            mapped = dict(_SOURCE_PRIORITY).get(source_list)
            state = mapped or STATE_UNKNOWN
        else:
            state = STATE_UNKNOWN

    # Freshness fail-closed: never promote missing/stale evidence into active success.
    if freshness == "stale" and state in {
        STATE_RUNNING,
        STATE_COMPLETED,
        STATE_RECOVERING,
    }:
        return STATE_STALE
    if freshness == "unknown" and state in {STATE_RUNNING, STATE_COMPLETED}:
        return STATE_UNCONFIRMED
    if item.get("provider_confirmed") is False and state == STATE_COMPLETED:
        return STATE_UNCONFIRMED
    return state


def _waiting_on_text(state: str, item: dict[str, Any]) -> str | None:
    if state == STATE_WAITING_ON_YOU:
        return "Founder judgment"
    if state == STATE_WAITING_ON_PROVIDER:
        return _norm(item.get("provider_name")) or "External provider"
    if state == STATE_BLOCKED:
        return _norm(item.get("failure_reason") or item.get("escalation_reason")) or "Blocked"
    if state == STATE_DEGRADED:
        return "Runtime or provider degradation"
    if state == STATE_RECOVERING:
        return None
    if state == STATE_DUPLICATE_SUPPRESSED:
        return None
    return _norm(item.get("waiting_on"))


def _what_happened(item: dict[str, Any], state: str) -> str:
    if state == STATE_DUPLICATE_SUPPRESSED:
        return (
            "An equivalent governed action already existed, so duplicate work was "
            "suppressed. This is not confirmation that a message was sent or a "
            "meeting was booked."
        )
    if state == STATE_RECOVERING:
        return (
            "Automatic recovery may continue without founder action. Recovery is "
            "not the same as normal running and is not confirmed success."
        )
    if state == STATE_WAITING_ON_PROVIDER:
        return (
            "Work is waiting on an external provider. Provider silence is not "
            "success, failure, or a founder-required decision."
        )
    if state == STATE_WAITING_ON_YOU:
        return "A governed proposal or escalation is waiting for founder judgment."
    if state == STATE_COMPLETED:
        return "Governed work reported completion in existing repository evidence."
    if state == STATE_BLOCKED:
        return _norm(item.get("failure_reason") or item.get("escalation_reason")) or (
            "Work is blocked according to existing orchestration evidence."
        )
    if state == STATE_STALE:
        return "Available evidence is out of date. Current state cannot be confirmed."
    if state in {STATE_UNKNOWN, STATE_UNCONFIRMED}:
        return "State cannot be confirmed from available evidence."
    label = _norm(item.get("label"))
    if label:
        return label
    return f"{_objective_for(item)} — {_STATE_LABELS.get(state, state)}"


def _what_did_not_happen(state: str) -> list[str]:
    notes: list[str] = []
    if state == STATE_DUPLICATE_SUPPRESSED:
        notes.extend(
            [
                "Message was not sent by this suppression event.",
                "Meeting was not booked by this suppression event.",
                "Provider success was not confirmed.",
            ]
        )
    elif state == STATE_RECOVERING:
        notes.extend(
            [
                "Completion is not confirmed.",
                "Normal healthy running is not confirmed.",
            ]
        )
    elif state == STATE_WAITING_ON_PROVIDER:
        notes.extend(
            [
                "Provider success is not confirmed.",
                "Founder action is not implied by provider wait.",
            ]
        )
    elif state in {STATE_STALE, STATE_UNKNOWN, STATE_UNCONFIRMED}:
        notes.append("No confirmed running, completed, sent, booked, or approved state.")
    elif state == STATE_WAITING_ON_YOU:
        notes.append("Nothing has executed from this wait — judgment is still required.")
    return notes


def _next_when_supported(state: str, item: dict[str, Any]) -> str | None:
    """Only emit next-step text when deterministically supported — never fabricate."""
    if state == STATE_WAITING_ON_YOU:
        return "Review the related Decision and approve or reject via the existing path."
    if state == STATE_RECOVERING:
        if item.get("recovery_execution_allowed") is False:
            return None
        return "Automatic recovery may continue. Completion is not yet confirmed."
    if state == STATE_RUNNING:
        mode = (_norm(item.get("execution_mode")) or "").upper()
        if mode and mode != "AUTONOMOUS":
            return None
        return "Work may continue without founder action."
    if state == STATE_WAITING_ON_PROVIDER:
        return "Wait for provider confirmation. Do not treat silence as success."
    if state == STATE_COMPLETED:
        return None
    return None


def _pending_map(pending_approvals: list[Any] | None, org: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for raw in pending_approvals or []:
        if not isinstance(raw, dict):
            continue
        row_org = _norm(raw.get("organization_id"))
        if row_org and row_org != org:
            continue
        rid = _norm(raw.get("id") or raw.get("request_id"))
        if rid:
            out[rid] = raw
    return out


def _row_org_ok(item: dict[str, Any], org: str) -> bool:
    row_org = _norm(item.get("organization_id"))
    if row_org and row_org != org:
        return False
    return True


def _collect_orch_items(orch: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    items: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for list_key, _state in _SOURCE_PRIORITY:
        for raw in orch.get(list_key) or []:
            if not isinstance(raw, dict):
                continue
            key = _identity(raw, list_key)
            if key in seen:
                continue
            # Prefer recovering over failed for the same work_id.
            if list_key == "failed":
                wid = _norm(raw.get("work_id"))
                retryable = orch.get("retryable") or []
                if wid and any(
                    isinstance(r, dict) and _norm(r.get("work_id")) == wid for r in retryable
                ):
                    continue
            seen.add(key)
            items.append((list_key, raw))
    # Never treat orch["active"] as running progress.
    _ = orch.get(_ORCH_FAILED_ALIAS_KEY)
    return items


def _activity_items_from_logs(
    recent_activity: list[Any] | None,
    *,
    org: str,
    seen: set[str],
) -> list[tuple[str, dict[str, Any]]]:
    items: list[tuple[str, dict[str, Any]]] = []
    for raw in recent_activity or []:
        if not isinstance(raw, dict):
            continue
        if not _row_org_ok(raw, org):
            # Activity events may omit organization_id when already org-scoped by builder.
            if _norm(raw.get("organization_id")) and _norm(raw.get("organization_id")) != org:
                continue
        key = _identity(raw, "activity")
        if key in seen:
            continue
        # Only surface activity rows that carry agent/work semantics or suppression.
        if not (
            _norm(raw.get("actor"))
            or _norm(raw.get("assigned_agent"))
            or _is_duplicate_suppressed(raw)
            or _norm(raw.get("action_type"))
        ):
            continue
        seen.add(key)
        items.append(("activity", raw))
    return items


def _compose_card(
    *,
    org: str,
    source_list: str,
    item: dict[str, Any],
    pending_by_id: dict[str, dict[str, Any]],
    pause: dict[str, Any],
) -> dict[str, Any]:
    activity_key = _identity(item, source_list)
    freshness = _freshness_for(item)
    state = _normalize_state(source_list=source_list, item=item, freshness=freshness)

    # Runtime gate degradation without fabricating agent work.
    if pause.get("acp2_execution_killed") or pause.get("heartbeat_paused"):
        if state == STATE_RUNNING:
            state = STATE_DEGRADED

    approval_id = _approval_id_for(item, pending_by_id)
    if source_list == "awaiting_human" or (
        approval_id and state not in {STATE_COMPLETED, STATE_DUPLICATE_SUPPRESSED}
    ):
        # Awaiting human / linked pending AR is WAITING_ON_YOU — never RUNNING.
        if state in {STATE_RUNNING, STATE_RECOVERING, STATE_UNKNOWN}:
            state = STATE_WAITING_ON_YOU

    agent_name = _agent_display_name(item)
    founder_required = state == STATE_WAITING_ON_YOU
    provider_awaited = state == STATE_WAITING_ON_PROVIDER
    continues = state in {STATE_RUNNING, STATE_RECOVERING} and not founder_required

    href_decision = f"/decisions/{approval_id}" if approval_id else None
    card = {
        "activity_key": activity_key,
        "organization_id": org,
        "agent_name": agent_name,
        "agent_role": _humanize(
            item.get("work_kind") or item.get("role") or item.get("action_type"),
            fallback="Governed agent",
        ),
        "objective": _objective_for(item),
        "account_context": _context_for(item),
        "normalized_state": state,
        "state_label": _STATE_LABELS.get(state, state.replace("_", " ").title()),
        "waiting_on": _waiting_on_text(state, item),
        "latest_event": _norm(item.get("label"))
        or _norm(item.get("action_type"))
        or _objective_for(item),
        "occurred_at": _norm(item.get("created_at") or item.get("occurred_at")),
        "freshness": freshness,
        "founder_action_required": founder_required,
        "provider_awaited": provider_awaited,
        "continues_autonomously": continues,
        "href_detail": f"/agents/activity/{encode_activity_key(activity_key)}",
        "href_decision": href_decision,
        "approval_request_id": approval_id,
        "canonical_work_id": _norm(item.get("work_id")),
        "canonical_event_id": _norm(item.get("id") or item.get("event_id")),
        "action_type": _norm(item.get("action_type")),
        "execution_mode": _norm(item.get("execution_mode")),
        "provenance_action_type": _norm(item.get("action_type") or item.get("requested_effect")),
        # Presentation grouping only — not persistent Agent / Situation identity.
        "presentation_agent_key": f"agent:{agent_name.lower()}",
        "presentation_activity_key": activity_key,
        "source_list": source_list,
        "persistent": False,
    }
    return card


def compose_agents_overview(
    *,
    organization_id: str | None,
    agent_orchestration: dict[str, Any] | None = None,
    recent_activity: list[Any] | None = None,
    pending_approvals: list[Any] | None = None,
) -> dict[str, Any]:
    """Build Agents Overview from existing orch + activity + pending approvals."""
    org = _norm(organization_id)
    if not org:
        return _empty_overview(organization_id=None, reason="missing_organization_id")

    orch = agent_orchestration if isinstance(agent_orchestration, dict) else {}
    # Tenant fence on orchestration payload when present.
    orch_org = _norm(orch.get("organization_id"))
    if orch_org and orch_org != org:
        return _empty_overview(organization_id=org, reason="organization_mismatch")

    pending_by_id = _pending_map(pending_approvals, org)
    pause = orch.get("pause") if isinstance(orch.get("pause"), dict) else {}

    cards: list[dict[str, Any]] = []
    seen: set[str] = set()

    for source_list, raw in _collect_orch_items(orch):
        if not _row_org_ok(raw, org):
            continue
        key = _identity(raw, source_list)
        if key in seen:
            continue
        seen.add(key)
        cards.append(
            _compose_card(
                org=org,
                source_list=source_list,
                item=raw,
                pending_by_id=pending_by_id,
                pause=pause,
            )
        )

    for source_list, raw in _activity_items_from_logs(recent_activity, org=org, seen=seen):
        cards.append(
            _compose_card(
                org=org,
                source_list=source_list,
                item=raw,
                pending_by_id=pending_by_id,
                pause=pause,
            )
        )

    # Provider / runtime degradation banner card when gates killed and no items.
    if (pause.get("acp2_execution_killed") or pause.get("heartbeat_paused")) and not cards:
        cards.append(
            {
                "activity_key": "runtime:degraded_gates",
                "organization_id": org,
                "agent_name": "Agent runtime",
                "agent_role": "Runtime gate",
                "objective": "Agent execution degraded",
                "account_context": None,
                "normalized_state": STATE_DEGRADED,
                "state_label": _STATE_LABELS[STATE_DEGRADED],
                "waiting_on": "Runtime or provider degradation",
                "latest_event": "Heartbeat paused or autonomous execution killed",
                "occurred_at": None,
                "freshness": "unknown",
                "founder_action_required": False,
                "provider_awaited": False,
                "continues_autonomously": False,
                "href_detail": f"/agents/activity/{encode_activity_key('runtime:degraded_gates')}",
                "href_decision": None,
                "approval_request_id": None,
                "canonical_work_id": None,
                "canonical_event_id": None,
                "action_type": None,
                "execution_mode": None,
                "provenance_action_type": None,
                "presentation_agent_key": "agent:runtime",
                "presentation_activity_key": "runtime:degraded_gates",
                "source_list": "runtime_gate",
                "persistent": False,
            }
        )

    # Newest first when timestamps exist.
    with_ts = [c for c in cards if c.get("occurred_at")]
    without = [c for c in cards if not c.get("occurred_at")]
    with_ts.sort(key=lambda c: str(c.get("occurred_at")), reverse=True)
    cards = with_ts + without

    # Presentation-only agent rollup (not a registry / SoT).
    agents_map: dict[str, dict[str, Any]] = {}
    for card in cards:
        pkey = str(card.get("presentation_agent_key") or "agent:unknown")
        bucket = agents_map.setdefault(
            pkey,
            {
                "presentation_agent_key": pkey,
                "agent_name": card.get("agent_name"),
                "activity_count": 0,
                "states": [],
                "persistent": False,
            },
        )
        bucket["activity_count"] += 1
        bucket["states"].append(card.get("normalized_state"))

    return {
        "organization_id": org,
        "source": "command_v2_agents",
        "persistent": False,
        "fail_closed_reason": None,
        "activity_items": cards,
        "count": len(cards),
        "agents": list(agents_map.values()),
        "agent_count": len(agents_map),
    }


def compose_agent_activity_detail(
    *,
    organization_id: str | None,
    activity_key: str | None,
    agent_orchestration: dict[str, Any] | None = None,
    recent_activity: list[Any] | None = None,
    pending_approvals: list[Any] | None = None,
) -> dict[str, Any]:
    """Compose Agent Activity Detail for one presentation activity key."""
    org = _norm(organization_id)
    if not org:
        return _empty_detail(organization_id=None, reason="missing_organization_id")

    key = decode_activity_key(activity_key) if activity_key else None
    key = _norm(key)
    if not key:
        return _empty_detail(organization_id=org, reason="missing_activity_key")

    overview = compose_agents_overview(
        organization_id=org,
        agent_orchestration=agent_orchestration,
        recent_activity=recent_activity,
        pending_approvals=pending_approvals,
    )
    if overview.get("fail_closed_reason"):
        return _empty_detail(
            organization_id=org,
            reason=str(overview.get("fail_closed_reason")),
        )

    match: dict[str, Any] | None = None
    for item in overview.get("activity_items") or []:
        if not isinstance(item, dict):
            continue
        if _norm(item.get("activity_key")) == key:
            match = item
            break
        if _norm(item.get("presentation_activity_key")) == key:
            match = item
            break

    if match is None:
        return _empty_detail(organization_id=org, reason="activity_not_found_for_tenant")

    # Cross-tenant card must never surface.
    if _norm(match.get("organization_id")) != org:
        return _empty_detail(organization_id=org, reason="organization_mismatch")

    state = str(match.get("normalized_state") or STATE_UNKNOWN)
    approval_id = _norm(match.get("approval_request_id"))
    related_decision = None
    actions: list[dict[str, Any]] = []
    if approval_id and match.get("founder_action_required"):
        related_decision = {
            "approval_request_id": approval_id,
            "href": f"/decisions/{approval_id}",
            "label": "View Decision",
        }
        # Prefer navigation to frozen I3 Decision Detail — no new mutation path.
        actions.append(
            {
                "key": f"view_decision:{approval_id}",
                "label": "View Decision",
                "action_type": "navigate",
                "href": f"/decisions/{approval_id}",
                "reason": "Open the canonical Decision Detail. No approval authority on this page.",
            }
        )

    what_next = _next_when_supported(state, match)
    did_not = _what_did_not_happen(state)

    return {
        "organization_id": org,
        "source": "command_v2_agents",
        "persistent": False,
        "fail_closed_reason": None,
        "available": True,
        "activity_key": key,
        "presentation_activity_key": key,
        "presentation_agent_key": match.get("presentation_agent_key"),
        "agent_name": match.get("agent_name"),
        "agent_role": match.get("agent_role"),
        "objective": {
            "text": match.get("objective"),
            "account_context": match.get("account_context"),
        },
        "current_state": {
            "normalized_state": state,
            "state_label": match.get("state_label"),
            "freshness": match.get("freshness"),
            "founder_action_required": bool(match.get("founder_action_required")),
            "provider_awaited": bool(match.get("provider_awaited")),
            "continues_autonomously": bool(match.get("continues_autonomously")),
        },
        "what_happened": {
            "text": _what_happened(match, state),
            "latest_event": match.get("latest_event"),
            "occurred_at": match.get("occurred_at"),
        },
        "why_stopped_or_continues": {
            "text": match.get("waiting_on")
            or (
                "Automatic recovery may continue."
                if state == STATE_RECOVERING
                else (
                    "Work may continue without founder action."
                    if state == STATE_RUNNING
                    else _what_happened(match, state)
                )
            )
        },
        "waiting_on": {
            "text": match.get("waiting_on"),
            "founder_action_required": bool(match.get("founder_action_required")),
            "provider_awaited": bool(match.get("provider_awaited")),
        },
        "what_happens_next": {
            "available": what_next is not None,
            "text": what_next,
        },
        "provenance": {
            "canonical_work_id": match.get("canonical_work_id"),
            "canonical_event_id": match.get("canonical_event_id"),
            "action_type": match.get("action_type"),
            "execution_mode": match.get("execution_mode"),
            "provenance_action_type": match.get("provenance_action_type"),
            "source_list": match.get("source_list"),
            "occurred_at": match.get("occurred_at"),
        },
        "related_decision": related_decision,
        "what_did_not_happen": did_not,
        "authority": {
            "exists": [
                "Presentation of existing governed orchestration and activity evidence.",
                "Linking to canonical Decision Detail when an ApprovalRequest exists.",
            ],
            "does_not_exist": [
                "This page does not approve, reject, send, book, or execute.",
                "Opening Agent Activity performs no mutation.",
                "Presentation keys are not persistent Agent or Situation identity.",
                "Unknown agents gain no new authority by appearing here.",
            ],
        },
        "actions": actions,
        "href_overview": "/agents",
    }
