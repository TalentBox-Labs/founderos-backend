"""Compose enterprise workspace view models from existing Founder OS reads.

Every datum is tagged. Failed reads become error states. Missing reads become
API_GAP states. This module does not write, publish, send mail, or start Hermes.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy import text

from revenue_os.agents.orchestration import AgentCoordinator
from revenue_os.database import SessionLocal
from revenue_os.models.activity import Activity
from revenue_os.services.credentials_vault import list_configured_connectors
from revenue_os.services.deal_automation_service import get_pipeline_health
from revenue_os.services.founder_ui_read_model import (
    build_activity_snapshot,
    build_approvals_snapshot,
    build_command_center_snapshot,
    build_demand_contacts_snapshot,
)
from revenue_os.services.gmail_beta_freeze import gmail_beta_frozen, gmail_beta_frozen_detail
from revenue_os.services.marketing_campaigns import list_campaigns
from revenue_os.services.operator_flow_read_model import build_operator_flow_snapshot
from revenue_os.services.orchestration_runtime import backend_status
from revenue_os.services.tenant_scoped_access import verify_activity_in_tenant
from revenue_os.scheduler import heartbeat_enabled, scheduler
from runner_api_routers.content_studio import build_content_list
from runner_api_routers.crm import _activity_dict
from runner_api_routers.editorial import build_editorial_pending
from runner_api_routers.integrations import CONNECTOR_CATALOG
from runner_api_routers.marketing import _marketing_integration_status, _marketing_runs
from src.tools import publishing_engine as pe
from src.ui.content_ops_beta.presenter import present_content_ops_read
from src.ui.content_ops_beta.reader import get_content_ops_reader
from src.ui.enterprise.catalog import API_GAPS

logger = logging.getLogger(__name__)

_PUBLICATION_TRUTH_GAP = (
    "QA pass, editorial approval, tracker status, and publishing job state are not publication. "
    "Canonical publication truth is the live current-week read."
)


def compose_surface(
    surface_id: str,
    *,
    organization_id: str | None,
    identity: dict[str, Any] | None,
    tenant: dict[str, Any] | None,
    active_week: str,
) -> dict[str, Any]:
    builder = _BUILDERS.get(surface_id)
    if builder is None:
        raise KeyError(surface_id)
    view = builder(
        organization_id=organization_id,
        identity=identity,
        tenant=tenant,
        active_week=active_week,
    )
    view["surface_id"] = surface_id
    view.setdefault("gaps", [])
    view.setdefault("sources", [])
    view.setdefault("kpis", [])
    view.setdefault("decisions", [])
    view.setdefault("alerts", [])
    view.setdefault("table", None)
    view.setdefault("pipeline", None)
    view.setdefault("primary_action", None)
    view.setdefault("design_system", False)
    if surface_id.startswith("content_") or surface_id == "marketing_performance":
        _attach_live_publication(view)
    return view


def _guard(source: str, fn: Any) -> dict[str, Any]:
    try:
        return {"state": "ok", "value": fn(), "message": ""}
    except Exception:
        logger.warning("Enterprise read failed: %s", source, exc_info=True)
        return {
            "state": "error",
            "value": None,
            "message": f"{source} failed. No substitute data was loaded.",
        }


def _current_week_publication() -> dict[str, Any]:
    """Live current-week publication tokens. A failed read does not load the mock."""
    loaded = _guard(
        "Content Ops current week",
        lambda: present_content_ops_read(get_content_ops_reader().read_current_week(None)),
    )
    if loaded["state"] != "ok":
        return {"ok": False, "detail": loaded["message"]}
    presented = loaded["value"] or {}
    if presented.get("source") == "mock" or presented.get("live") is not True:
        return {
            "ok": False,
            "detail": "The current-week read was not the live publication-truth reader. No fixture was shown.",
        }
    state = presented.get("read_state")
    if state == "ready":
        publication = presented.get("publication") or {}
        return {
            "ok": True,
            "state": "ready",
            "week_id": str(presented.get("week_id") or ""),
            "truth": str(publication.get("truth") or ""),
            "verification": str(publication.get("verification") or ""),
            "status": str(publication.get("status") or ""),
            "url": str(publication.get("url") or ""),
            "proven": bool(publication.get("show_success_link")),
        }
    if state == "empty":
        return {
            "ok": True,
            "state": "empty",
            "detail": str(presented.get("empty_message") or "No active week"),
        }
    error = presented.get("error") or {}
    return {
        "ok": False,
        "detail": str(
            error.get("message")
            or "Current-week publication read failed. No substitute was loaded."
        ),
    }


def _replace_verified_kpi(view: dict[str, Any], value: str, why: str) -> None:
    replaced = False
    kpis: list[dict[str, Any]] = []
    for kpi in view.get("kpis") or []:
        if kpi.get("label") == "Verified published" and not replaced:
            kpis.append(_kpi("Verified published", value, why, source="LIVE_API"))
            replaced = True
            continue
        kpis.append(kpi)
    if not replaced:
        kpis.append(_kpi("Verified published", value, why, source="LIVE_API"))
    view["kpis"] = kpis


def _attach_live_publication(view: dict[str, Any]) -> None:
    sources = list(view.get("sources") or [])
    if "LIVE_API: content ops current week" not in sources:
        sources.append("LIVE_API: content ops current week")
    view["sources"] = sources
    publication = _current_week_publication()
    if not publication.get("ok"):
        view["alerts"].append(
            _alert("danger", "Current-week publication read", str(publication.get("detail") or ""))
        )
        _replace_verified_kpi(
            view,
            "Unavailable",
            "The current-week read failed. No fixture was substituted.",
        )
        return
    if publication.get("state") == "empty":
        view["alerts"].append(_alert("info", "No active week", str(publication.get("detail") or "")))
        _replace_verified_kpi(view, "No", "No active week, so nothing is verified published.")
        return
    proven = "Yes" if publication["proven"] else "No"
    why = (
        f"Week {publication['week_id']}. "
        f"publication_truth={publication['truth']}. "
        f"verification_status={publication['verification']}. "
        f"publication_status={publication['status']}."
    )
    if publication["url"]:
        why += f" published_url={publication['url']}."
    else:
        why += " No published URL is on this read."
    _replace_verified_kpi(view, proven, why)
    view["kpis"].extend(
        [
            _kpi(
                "Publication truth",
                publication["truth"],
                "Live current-week token. Not QA, editorial, or tracker status.",
                source="LIVE_API",
            ),
            _kpi(
                "Verification status",
                publication["verification"],
                "Live current-week verification token.",
                source="LIVE_API",
            ),
            _kpi(
                "Publication status",
                publication["status"],
                "Live current-week publication status.",
                source="LIVE_API",
            ),
            _kpi(
                "Published URL",
                publication["url"] or "None",
                "Shown only when the current-week read includes a safe URL.",
                source="LIVE_API",
            ),
        ]
    )


def _org_block(organization_id: str | None) -> dict[str, Any] | None:
    if organization_id:
        return None
    return {
        "state": "unavailable",
        "message": "Organization context required. Records were not loaded.",
    }


def _base(
    *,
    workspace: str,
    title: str,
    section: str,
    purpose: str,
    tabs: list[dict[str, str]],
    active_page: str,
    read_state: str,
) -> dict[str, Any]:
    marked = []
    for tab in tabs:
        item = dict(tab)
        item["current"] = "true" if tab["id"] == section else "false"
        marked.append(item)
    return {
        "workspace": workspace,
        "title": title,
        "section": section,
        "purpose": purpose,
        "tabs": marked,
        "active_page": active_page,
        "read_state": read_state,
        "breadcrumb": [workspace, title],
    }


def _kpi(
    label: str,
    value: str,
    why: str,
    *,
    action_href: str | None = None,
    action_label: str | None = None,
    source: str,
) -> dict[str, Any]:
    return {
        "label": label,
        "value": value,
        "why": why,
        "action_href": action_href,
        "action_label": action_label,
        "source": source,
    }


def _table(
    caption: str,
    columns: list[dict[str, str]],
    rows: list[dict[str, str]],
    empty: str,
) -> dict[str, Any]:
    return {"caption": caption, "columns": columns, "rows": rows, "empty": empty}


def _alert(tone: str, title: str, body: str) -> dict[str, str]:
    return {"tone": tone, "title": title, "body": body}


def _home_tabs() -> list[dict[str, str]]:
    return []


def _revenue_tabs(current_href_id: str) -> list[dict[str, str]]:
    del current_href_id
    return [
        {"id": "overview", "label": "Overview", "href": "/os/revenue"},
        {"id": "contacts", "label": "Contacts", "href": "/os/revenue/contacts"},
        {"id": "companies", "label": "Companies", "href": "/os/revenue/companies"},
        {"id": "deals", "label": "Deals", "href": "/os/revenue/deals"},
        {"id": "pipeline", "label": "Pipeline", "href": "/os/revenue/pipeline"},
        {"id": "activities", "label": "Activities", "href": "/os/revenue/activities"},
        {"id": "prospecting", "label": "Prospecting", "href": "/os/revenue/prospecting"},
        {"id": "intelligence", "label": "Revenue Intelligence", "href": "/os/revenue/intelligence"},
    ]


def _marketing_tabs() -> list[dict[str, str]]:
    return [
        {"id": "overview", "label": "Overview", "href": "/os/marketing"},
        {"id": "campaigns", "label": "Campaigns", "href": "/os/marketing/campaigns"},
        {"id": "demand", "label": "Demand", "href": "/os/marketing/demand"},
        {"id": "seo", "label": "SEO", "href": "/os/marketing/seo"},
        {"id": "performance", "label": "Content Performance", "href": "/os/marketing/performance"},
        {"id": "analytics", "label": "Analytics", "href": "/os/marketing/analytics"},
    ]


def _content_tabs() -> list[dict[str, str]]:
    return [
        {"id": "overview", "label": "Overview", "href": "/os/content"},
        {"id": "calendar", "label": "Calendar", "href": "/os/content/calendar"},
        {"id": "studio", "label": "Studio", "href": "/os/content/studio"},
        {"id": "editorial", "label": "Editorial", "href": "/os/content/editorial"},
        {"id": "publishing", "label": "Publishing", "href": "/os/content/publishing"},
    ]


def _operations_tabs() -> list[dict[str, str]]:
    return [
        {"id": "overview", "label": "Overview", "href": "/os/operations"},
        {"id": "approvals", "label": "Approvals", "href": "/os/operations/approvals"},
        {"id": "activity", "label": "Activity", "href": "/os/operations/activity"},
        {"id": "automations", "label": "Workflows", "href": "/os/operations/automations"},
        {"id": "jobs", "label": "Jobs", "href": "/os/operations/jobs"},
    ]


def _agent_tabs() -> list[dict[str, str]]:
    return [
        {"id": "overview", "label": "Agent Hub", "href": "/os/agents"},
        {"id": "runs", "label": "Runs", "href": "/os/agents/runs"},
        {"id": "capabilities", "label": "Capabilities", "href": "/os/agents/capabilities"},
        {"id": "evaluations", "label": "Evaluations", "href": "/os/agents/evaluations"},
    ]


def _system_tabs() -> list[dict[str, str]]:
    return [
        {"id": "overview", "label": "Overview", "href": "/os/system"},
        {"id": "integrations", "label": "Integrations", "href": "/os/system/integrations"},
        {"id": "governance", "label": "Governance", "href": "/os/system/governance"},
        {"id": "identity", "label": "Identity", "href": "/os/system/identity"},
        {"id": "tenant", "label": "Tenant", "href": "/os/system/tenant"},
        {"id": "health", "label": "System Health", "href": "/os/system/health"},
        {"id": "settings", "label": "Settings", "href": "/os/system/settings"},
    ]


def _flow(organization_id: str | None) -> dict[str, Any]:
    blocked = _org_block(organization_id)
    if blocked is not None:
        return blocked
    return _guard(
        "Operator flow",
        lambda: build_operator_flow_snapshot(organization_id=organization_id),
    )


def _demand(organization_id: str | None) -> dict[str, Any]:
    blocked = _org_block(organization_id)
    if blocked is not None:
        return blocked
    return _guard(
        "Demand and contacts",
        lambda: build_demand_contacts_snapshot(organization_id=organization_id),
    )


def _hermes_status() -> dict[str, Any]:
    status = backend_status()
    configured = bool(status.get("hermes"))
    return {
        "configured": configured,
        "endpoint_present": configured,
        "authority": "none",
        "headline": "Hermes endpoint configured" if configured else "Hermes endpoint not configured",
        "detail": (
            "An endpoint is present in HERMES_AGENT_URL. "
            "This screen does not grant Hermes execution authority."
            if configured
            else "HERMES_AGENT_URL is not set. Hermes is not running and has no authority from this screen."
        ),
    }


def _publication_label(job_state: str) -> str:
    raw = (job_state or "").strip().lower()
    labels = {
        "editorial_approved": "Approved (editorial). Not publication.",
        "publish_pending": "Publication pending",
        "publishing": "Publication pending",
        "published": "Job recorded published. Verification unknown.",
        "failed": "Failed",
        "retry": "Publication pending",
        "cancelled": "Blocked",
    }
    return labels.get(raw, "Remote outcome unknown")


def _content_rows(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for item in items:
        cid = str(item.get("content_id") or "")
        rows.append(
            {
                "content_id": cid,
                "title": str(item.get("title") or "—"),
                "tracker_status": str(item.get("status") or "—"),
                "qa_status": str(item.get("qa_status") or "—"),
                "current_step": str(item.get("current_step") or "—"),
                "publication_truth": "Not the current-week read",
                "href": f"/content-studio/{cid}" if cid else "",
                "calendar_href": f"/weeks/{cid}" if cid else "",
            }
        )
    return rows


def _content_table(items: list[dict[str, Any]]) -> dict[str, Any]:
    return _table(
        "Content inventory from the tracker read. Publication truth is a separate column.",
        [
            {"key": "content_id", "label": "Content", "priority": "high"},
            {"key": "title", "label": "Title", "priority": "high"},
            {"key": "tracker_status", "label": "Tracker status", "priority": "medium"},
            {"key": "qa_status", "label": "QA", "priority": "medium"},
            {"key": "current_step", "label": "Step", "priority": "low"},
            {"key": "publication_truth", "label": "Publication truth", "priority": "high"},
        ],
        [
            {
                "content_id": row["content_id"],
                "title": row["title"],
                "tracker_status": row["tracker_status"],
                "qa_status": row["qa_status"],
                "current_step": row["current_step"],
                "publication_truth": row["publication_truth"],
                "_href": row["href"],
            }
            for row in _content_rows(items)
        ],
        "No content rows were returned by the tracker read.",
    )


def _tracker_items() -> dict[str, Any]:
    return _guard("Content tracker", build_content_list)


def _dict_get(node: Any, key: str, default: Any = None) -> Any:
    if isinstance(node, dict) and key in node:
        return node[key]
    return default


def build_home(**ctx: Any) -> dict[str, Any]:
    organization_id = ctx["organization_id"]
    view = _base(
        workspace="Home",
        title="Home",
        section="home",
        purpose=(
            "What needs a decision, what the command read says changed, what is blocked, "
            "and where to inspect revenue, content, and operations."
        ),
        tabs=_home_tabs(),
        active_page="home",
        read_state="ok",
    )
    view["alerts"] = []
    snapped = _guard(
        "Command center",
        lambda: build_command_center_snapshot(organization_id=organization_id),
    )
    view["sources"] = ["DERIVED_FROM_LIVE_API: build_command_center_snapshot"]
    if snapped["state"] != "ok" or not isinstance(snapped["value"], dict):
        view["read_state"] = snapped["state"]
        view["alerts"] = [_alert("danger", "Command center unavailable", snapped["message"])]
        return view
    snapshot = snapped["value"]
    if snapshot.get("state") == "unavailable":
        view["read_state"] = "unavailable"
        view["alerts"] = [
            _alert(
                "warning",
                "Organization context required",
                "Tenant-scoped decisions stay unloaded until a workspace is resolved.",
            )
        ]
        return view
    if snapshot.get("errors"):
        view["alerts"].append(
            _alert(
                "warning",
                "Partial read",
                "Some command sources failed: " + ", ".join(str(item) for item in snapshot["errors"]),
            )
        )
    loop = _dict_get(snapshot, "decision_loop", {})
    orchestration = _dict_get(snapshot, "agent_orchestration", {})
    counts = _dict_get(orchestration, "counts", {})
    pipeline = _dict_get(snapshot, "pipeline", {})
    recent = snapshot.get("recent_activity")
    publication = _current_week_publication()
    if publication.get("ok") and publication.get("proven") is True:
        publication_value = "verified"
        publication_why = "Authoritative current-week read returned verified publication."
    elif publication.get("ok") and publication.get("state") == "ready":
        publication_value = str(publication.get("truth") or "unproven")
        publication_why = "Current-week publication truth. Unverified tokens stay unverified."
    elif publication.get("ok"):
        publication_value = "unproven"
        publication_why = str(publication.get("detail") or "No verified publication was returned.")
    else:
        publication_value = "unavailable"
        publication_why = str(publication.get("detail") or "Current-week publication read failed.")
    view["kpis"] = [
        _kpi(
            "Needs a decision",
            str(_dict_get(loop, "requires_founder", "—")),
            "Items the command read marked as requiring a founder.",
            action_href="#decision-queue",
            action_label="Review decisions",
            source="DERIVED_FROM_LIVE_API",
        ),
        _kpi(
            "Recent activity rows",
            str(len(recent)) if isinstance(recent, list) else "—",
            "Rows returned by the command activity read. This is not a trend.",
            action_href="/os/operations/activity",
            action_label="Open activity",
            source="DERIVED_FROM_LIVE_API",
        ),
        _kpi(
            "Blocked agent work",
            str(_dict_get(counts, "blocked", "—")),
            "Blocked orchestration records for this organization. This is not Hermes authority.",
            action_href="/os/agents/runs",
            action_label="Open agent status",
            source="DERIVED_FROM_LIVE_API",
        ),
        _kpi(
            "Approvals waiting",
            str(snapshot.get("pending_approval_count", "—")),
            "Pending governed approvals. Opening the inbox does not approve them.",
            action_href="/pending-approvals",
            action_label="Open Approvals",
            source="LIVE_API",
        ),
        _kpi(
            "Commercial records",
            f"{_dict_get(pipeline, 'contacts', '—')} contacts · {_dict_get(pipeline, 'deals', '—')} deals",
            "Counts from the command pipeline read for this organization.",
            action_href="/os/revenue",
            action_label="Open revenue",
            source="DERIVED_FROM_LIVE_API",
        ),
        _kpi(
            "Publication truth",
            publication_value,
            publication_why,
            action_href="/os/content",
            action_label="Open content",
            source="LIVE_API" if publication.get("ok") else "API_GAP",
        ),
    ]
    decisions = []
    raw_items = snapshot.get("decision_items") or []
    if not isinstance(raw_items, list):
        raw_items = []
    for item in raw_items[:12]:
        if not isinstance(item, dict):
            continue
        actions = []
        raw_actions = item.get("command_actions") or []
        if not isinstance(raw_actions, list):
            raw_actions = []
        for action in raw_actions:
            if not isinstance(action, dict):
                continue
            mode = action.get("execution_mode")
            if mode == "INLINE_GOVERNED":
                actions.append(
                    {
                        "label": action.get("label") or "Governed action",
                        "href": "/command",
                        "state": "permitted-on-command-center",
                        "detail": "The post stays on Command Center, which already binds this action to server authority.",
                    }
                )
            elif action.get("href"):
                actions.append(
                    {
                        "label": action.get("label") or "Open",
                        "href": action.get("href"),
                        "state": "permitted",
                        "detail": action.get("reason") or "",
                    }
                )
        decisions.append(
            {
                "severity": item.get("authority_state") or "unknown",
                "severity_label": item.get("authority_state_label") or "Unknown",
                "domain": item.get("kind") or "unknown",
                "entity": " · ".join(
                    part for part in (item.get("person_label"), item.get("company_label")) if part
                )
                or "—",
                "title": item.get("title") or "Untitled decision",
                "reason": item.get("reason") or "",
                "evidence": item.get("outcome") or item.get("proposed_action") or "",
                "actions": actions,
                "blocked": "No permitted action was returned for this item." if not actions else "",
                "authority": item.get("authority_state_label") or "Unknown",
                "audit_href": "/activity",
            }
        )
    view["decisions"] = decisions
    if not decisions:
        view["alerts"].append(
            _alert("info", "Decision queue empty", "The command read returned no decision items.")
        )
    view["next_steps"] = [
        {
            "href": "/command",
            "label": "Command Center",
            "detail": "Governed decision actions stay on the command surface.",
        },
        {
            "href": "/cockpit",
            "label": "Executive Cockpit",
            "detail": "Attention panels from the existing cockpit read.",
        },
        {
            "href": "/os/content/pipeline",
            "label": "Content pipeline",
            "detail": "Pipeline remains a contextual action. It does not publish.",
        },
        {
            "href": "/os/system",
            "label": "System",
            "detail": "Integrations, agent registry, governance, and health.",
        },
    ]
    approval_count = snapshot.get("pending_approval_count") or 0
    try:
        approvals_waiting = int(approval_count)
    except (TypeError, ValueError):
        approvals_waiting = 0
    if approvals_waiting:
        view["primary_action"] = {"href": "/pending-approvals", "label": "Review approvals"}
    elif decisions:
        view["primary_action"] = {"href": "#decision-queue", "label": "Review decisions"}
    else:
        view["primary_action"] = {"href": "/os/revenue", "label": "Inspect revenue"}
    return view


def build_revenue_overview(**ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Sales",
        title="Overview",
        section="overview",
        purpose="Exceptions in demand, people, and deals for this organization.",
        tabs=_revenue_tabs("overview"),
        active_page="revenue",
        read_state="ok",
    )
    flow = _flow(ctx["organization_id"])
    view["sources"] = ["DERIVED_FROM_LIVE_API: build_operator_flow_snapshot"]
    if flow["state"] != "ok":
        view["read_state"] = flow["state"]
        view["alerts"] = [_alert("warning" if flow["state"] == "unavailable" else "danger", "Revenue read", flow["message"])]
        return view
    data = flow["value"] or {}
    attention = data.get("attention") or {}
    if attention.get("state") == "unavailable":
        view["read_state"] = "error"
        view["alerts"] = [_alert("danger", "Revenue read", attention.get("message") or "Operator sources unavailable")]
        return view
    contacts = data.get("contacts") or []
    deals = data.get("deals") or []
    pending = data.get("pending_demands") or []
    view["kpis"] = [
        _kpi("Demand waiting", str(len(pending)), "Pending qualified-demand handoffs.", action_href="/demand", action_label="Review demand", source="DERIVED_FROM_LIVE_API"),
        _kpi("Contacts", str(len(contacts)), "People in this organization.", action_href="/os/revenue/contacts", action_label="Open contacts", source="DERIVED_FROM_LIVE_API"),
        _kpi("Deals", str(len(deals)), "Deals returned for this organization.", action_href="/os/revenue/deals", action_label="Open deals", source="DERIVED_FROM_LIVE_API"),
        _kpi("Companies", "Unknown", "No tenant-scoped companies list is mounted.", action_href="/os/revenue/companies", action_label="See the gap", source="API_GAP"),
    ]
    items = attention.get("items") or []
    view["table"] = _table(
        "Attention queue from the operator flow read.",
        [
            {"key": "title", "label": "Item", "priority": "high"},
            {"key": "reason", "label": "Why it matters", "priority": "high"},
        ],
        [
            {
                "title": str(item.get("label") or item.get("title") or item.get("kind") or "Attention item"),
                "reason": str(item.get("detail") or item.get("reason") or "—"),
            }
            for item in items
        ],
        "No attention items were returned.",
    )
    view["primary_action"] = {"href": "/operator", "label": "Open revenue workflow"}
    view["gaps"] = [API_GAPS[0]]
    return view


def build_revenue_contacts(**ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Sales",
        title="Contacts",
        section="contacts",
        purpose="People already in this organization. Search filters this table only.",
        tabs=_revenue_tabs("contacts"),
        active_page="revenue_contacts",
        read_state="ok",
    )
    demand = _demand(ctx["organization_id"])
    view["sources"] = ["DERIVED_FROM_LIVE_API: build_demand_contacts_snapshot"]
    if demand["state"] != "ok":
        view["read_state"] = demand["state"]
        view["alerts"] = [_alert("warning" if demand["state"] == "unavailable" else "danger", "Contacts", demand["message"])]
        return view
    data = demand["value"] or {}
    if data.get("state") == "unavailable":
        view["read_state"] = "unavailable"
        view["alerts"] = [_alert("warning", "Contacts", data.get("message") or "Organization context required")]
        return view
    contacts = data.get("contacts") or []
    view["kpis"] = [
        _kpi("Contacts", str(len(contacts)), "Count of people returned for this organization.", source="DERIVED_FROM_LIVE_API"),
    ]
    view["table"] = _table(
        "Contacts",
        [
            {"key": "name", "label": "Name", "priority": "high"},
            {"key": "email", "label": "Email", "priority": "high"},
            {"key": "company_name", "label": "Company on record", "priority": "medium"},
            {"key": "status", "label": "Status", "priority": "medium"},
            {"key": "lead_score", "label": "Score", "priority": "low"},
        ],
        [
            {
                "name": str(row.get("name") or "—"),
                "email": str(row.get("email") or "—"),
                "company_name": str(row.get("company_name") or "—"),
                "status": str(row.get("status") or "—"),
                "lead_score": str(row.get("lead_score") if row.get("lead_score") is not None else "—"),
                "_href": f"/contacts/{row.get('id')}" if row.get("id") else "",
            }
            for row in contacts
        ],
        "No contacts were returned for this organization.",
    )
    view["primary_action"] = {"href": "/demand", "label": "Open demand and contacts"}
    view["client_filter"] = "name"
    return view


def build_revenue_companies(**_ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Sales",
        title="Companies",
        section="companies",
        purpose="A companies workspace needs a tenant-scoped list. That read is not available.",
        tabs=_revenue_tabs("companies"),
        active_page="revenue_companies",
        read_state="gap",
    )
    view["gaps"] = [API_GAPS[0]]
    view["alerts"] = [
        _alert(
            "blocked",
            "Companies are not loaded",
            "No records are shown. Company names that appear on contacts stay on the Contacts workspace and are not a companies directory.",
        )
    ]
    view["sources"] = ["API_GAP"]
    view["table"] = _table(
        "Companies",
        [{"key": "name", "label": "Company", "priority": "high"}],
        [],
        "No companies endpoint is available for this workspace.",
    )
    return view


def build_revenue_deals(**ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Sales",
        title="Deals",
        section="deals",
        purpose="Deals returned by the organization-scoped operator flow.",
        tabs=_revenue_tabs("deals"),
        active_page="revenue_deals",
        read_state="ok",
    )
    flow = _flow(ctx["organization_id"])
    view["sources"] = ["DERIVED_FROM_LIVE_API: build_operator_flow_snapshot deals"]
    if flow["state"] != "ok":
        view["read_state"] = flow["state"]
        view["alerts"] = [_alert("warning" if flow["state"] == "unavailable" else "danger", "Deals", flow["message"])]
        return view
    deals = (flow["value"] or {}).get("deals") or []
    view["table"] = _table(
        "Deals",
        [
            {"key": "name", "label": "Deal", "priority": "high"},
            {"key": "stage", "label": "Stage", "priority": "high"},
            {"key": "value", "label": "Value", "priority": "medium"},
            {"key": "contact_link", "label": "Contact link", "priority": "low"},
        ],
        [
            {
                "name": str(deal.get("name") or "—"),
                "stage": str(deal.get("stage") or "—"),
                "value": str(deal.get("value") if deal.get("value") is not None else "—"),
                "contact_link": str(deal.get("contact_link") or "—"),
                "_href": f"/contacts/{deal.get('contact_id')}" if deal.get("contact_id") else "",
            }
            for deal in deals
        ],
        "No deals were returned for this organization.",
    )
    view["primary_action"] = {"href": "/operator", "label": "Open revenue workflow"}
    return view


def build_revenue_pipeline(**ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Sales",
        title="Pipeline",
        section="pipeline",
        purpose="Open-deal stage counts for this organization. Closed deals are excluded by the pipeline health read.",
        tabs=_revenue_tabs("pipeline"),
        active_page="revenue_pipeline",
        read_state="ok",
    )
    blocked = _org_block(ctx["organization_id"])
    view["sources"] = ["LIVE_API: get_pipeline_health organization filter"]
    if blocked is not None:
        view["read_state"] = "unavailable"
        view["alerts"] = [_alert("warning", "Pipeline", blocked["message"])]
        return view
    def _load_health() -> dict[str, Any]:
        db = SessionLocal()
        try:
            return get_pipeline_health(db, organization_id=ctx["organization_id"])
        finally:
            db.close()

    health = _guard("Pipeline health", _load_health)
    view["read_state"] = health["state"] if health["state"] != "ok" else "ok"
    if health["state"] != "ok":
        view["alerts"] = [_alert("danger", "Pipeline", health["message"])]
        return view
    data = health["value"] or {}
    by_stage = data.get("by_stage") or {}
    view["kpis"] = [
        _kpi("Open deals", str(data.get("total_deals", "—")), "Deals with no close timestamp.", source="LIVE_API"),
        _kpi(
            "Open pipeline value",
            str(data.get("total_pipeline_value", "—")),
            "Sum of open deal value. Not a forecast commitment.",
            source="LIVE_API",
        ),
    ]
    view["pipeline"] = [
        {
            "stage": stage,
            "count": str(metrics.get("count", "—")),
            "value": str(metrics.get("total_value", "—")),
        }
        for stage, metrics in by_stage.items()
    ]
    if not view["pipeline"]:
        view["alerts"] = [_alert("info", "Pipeline empty", "No open deals were returned.")]
    return view


def build_revenue_activities(**ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Sales",
        title="Activities",
        section="activities",
        purpose="CRM activities that verify into this organization.",
        tabs=_revenue_tabs("activities"),
        active_page="revenue_activities",
        read_state="ok",
    )
    blocked = _org_block(ctx["organization_id"])
    view["sources"] = ["LIVE_API: Activity rows filtered with verify_activity_in_tenant"]
    if blocked is not None:
        view["read_state"] = "unavailable"
        view["alerts"] = [_alert("warning", "Activities", blocked["message"])]
        return view

    def _load() -> list[dict[str, Any]]:
        db = SessionLocal()
        try:
            rows = db.query(Activity).order_by(Activity.created_at.desc()).limit(100).all()
            kept = [row for row in rows if verify_activity_in_tenant(db, ctx["organization_id"], row)]
            return [_activity_dict(row) for row in kept]
        finally:
            db.close()

    loaded = _guard("CRM activities", _load)
    if loaded["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"] = [_alert("danger", "Activities", loaded["message"])]
        return view
    rows = loaded["value"] or []
    view["table"] = _table(
        "Activities",
        [
            {"key": "activity_type", "label": "Type", "priority": "high"},
            {"key": "subject", "label": "Subject", "priority": "high"},
            {"key": "status", "label": "Status", "priority": "medium"},
            {"key": "created_at", "label": "Created", "priority": "low"},
        ],
        [
            {
                "activity_type": str(row.get("activity_type") or "—"),
                "subject": str(row.get("subject") or "—"),
                "status": str(row.get("status") or "—"),
                "created_at": str(row.get("created_at") or "—"),
            }
            for row in rows
        ],
        "No activities verified into this organization.",
    )
    return view


def build_revenue_prospecting(**ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Sales",
        title="Prospecting",
        section="prospecting",
        purpose="People already stored for this organization. This page does not source, import, or send outreach.",
        tabs=_revenue_tabs("prospecting"),
        active_page="revenue_prospecting",
        read_state="ok",
    )
    view["sources"] = ["DERIVED_FROM_LIVE_API: build_demand_contacts_snapshot"]
    view["alerts"] = [
        _alert(
            "blocked",
            "Outreach stays on the server",
            "Gmail remains blocked. Opening the existing prospecting screen does not authorize a send from this page.",
        )
    ]
    view["primary_action"] = {"href": "/sales", "label": "Open prospecting screen"}
    demand = _demand(ctx["organization_id"])
    if demand["state"] != "ok":
        view["read_state"] = demand["state"]
        view["alerts"].append(
            _alert("warning" if demand["state"] == "unavailable" else "danger", "Prospecting", demand["message"])
        )
        return view
    data = demand["value"] or {}
    if data.get("state") == "unavailable":
        view["read_state"] = "unavailable"
        view["alerts"].append(_alert("warning", "Prospecting", data.get("message") or "Organization context required"))
        return view
    contacts = data.get("contacts") or []
    view["kpis"] = [
        _kpi(
            "People on record",
            str(len(contacts)),
            "Organization contacts. Not a newly sourced lead list.",
            source="DERIVED_FROM_LIVE_API",
        )
    ]
    view["table"] = _table(
        "People available to inspect. No sourcing run was executed.",
        [
            {"key": "name", "label": "Name", "priority": "high"},
            {"key": "email", "label": "Email", "priority": "high"},
            {"key": "status", "label": "Status", "priority": "medium"},
        ],
        [
            {
                "name": str(row.get("name") or "—"),
                "email": str(row.get("email") or "—"),
                "status": str(row.get("status") or "—"),
                "_href": f"/contacts/{row.get('id')}" if row.get("id") else "",
            }
            for row in contacts
        ],
        "No people were returned for this organization.",
    )
    return view


def build_revenue_intelligence(**ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Sales",
        title="Revenue Intelligence",
        section="intelligence",
        purpose="What the server will say about pipeline health, and what Hermes is actually allowed to do.",
        tabs=_revenue_tabs("intelligence"),
        active_page="revenue_intelligence",
        read_state="ok",
    )
    hermes = _hermes_status()
    view["hermes"] = hermes
    view["sources"] = [
        "LIVE_API: orchestration backend_status",
        "API_GAP: unscoped revenue intelligence rollup was not called",
    ]
    view["gaps"] = [API_GAPS[7]]
    view["alerts"] = [
        _alert("blocked", hermes["headline"], hermes["detail"]),
        _alert(
            "warning",
            "No intelligence rollup",
            "A global dashboard statistic exists in code and is not organization-filtered, so it is not shown.",
        ),
    ]
    blocked = _org_block(ctx["organization_id"])
    if blocked is not None:
        view["read_state"] = "unavailable"
        view["alerts"].append(_alert("warning", "Pipeline", blocked["message"]))
        return view

    def _load() -> dict[str, Any]:
        db = SessionLocal()
        try:
            return get_pipeline_health(db, organization_id=ctx["organization_id"])
        finally:
            db.close()

    health = _guard("Pipeline health", _load)
    if health["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"].append(_alert("danger", "Pipeline", health["message"]))
        return view
    data = health["value"] or {}
    view["kpis"] = [
        _kpi("Open deals", str(data.get("total_deals", "—")), "Organization-scoped open deals.", source="LIVE_API"),
        _kpi(
            "Weighted forecast",
            str(data.get("weighted_forecast", "—")),
            "Model output from open deals. Not a booked revenue number.",
            source="DERIVED_FROM_LIVE_API",
        ),
    ]
    return view


def build_marketing_overview(**ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Growth",
        title="Overview",
        section="overview",
        purpose="Campaigns, demand, and channel configuration. Cross-workspace revenue attribution is unknown.",
        tabs=_marketing_tabs(),
        active_page="marketing_os",
        read_state="ok",
    )
    view["sources"] = [
        "LIVE_API: marketing campaign list when an organization is present",
        "LIVE_API: marketing integration environment flags",
        "LIVE_API: output/marketing publish-status manifests",
    ]
    view["gaps"] = [API_GAPS[2]]
    view["alerts"] = [
        _alert(
            "unknown",
            "Attribution unknown",
            "Content, campaign, demand, contact, opportunity, and revenue are not joined by a Founder OS evidence record.",
        )
    ]
    integrations = _guard("Marketing integration flags", _marketing_integration_status)
    runs = _guard("Marketing run manifests", _marketing_runs)
    view["kpis"] = []
    if integrations["state"] == "ok":
        flags = integrations["value"] or {}
        configured = [name for name, on in flags.items() if on]
        missing = [name for name, on in flags.items() if not on]
        view["kpis"] = [
            _kpi(
                "Channels configured",
                str(len(configured)) if configured else "0",
                "Environment tokens present. Configuration is not permission to publish.",
                source="LIVE_API",
            ),
            _kpi(
                "Channels not configured",
                ", ".join(missing) if missing else "None",
                "Missing tokens. These channels cannot be treated as connected.",
                source="LIVE_API",
            ),
        ]
    else:
        view["alerts"].append(_alert("danger", "Integration flags", integrations["message"]))
    if runs["state"] == "ok":
        manifests = runs["value"] or []
        view["kpis"].append(
            _kpi(
                "Run manifests",
                str(len(manifests)),
                "Files under output/marketing. A manifest is not verified CMS publication.",
                action_href="/marketing",
                action_label="Open marketing agent",
                source="LIVE_API",
            )
        )
    else:
        view["alerts"].append(_alert("danger", "Run manifests", runs["message"]))
    blocked = _org_block(ctx["organization_id"])
    if blocked is not None:
        view["alerts"].append(_alert("warning", "Campaigns", blocked["message"]))
        return view
    campaigns = _guard(
        "Campaigns",
        lambda: list_campaigns(organization_id=ctx["organization_id"]),
    )
    if campaigns["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"].append(_alert("danger", "Campaigns", campaigns["message"]))
        return view
    view["kpis"].append(
        _kpi(
            "Campaigns",
            str(len(campaigns["value"] or [])),
            "Campaign rows stored for this organization.",
            action_href="/os/marketing/campaigns",
            action_label="Open campaigns",
            source="LIVE_API",
        )
    )
    view["primary_action"] = {"href": "/marketing", "label": "Open marketing agent"}
    return view


def build_marketing_campaigns(**ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Growth",
        title="Campaigns",
        section="campaigns",
        purpose="Campaign records for this organization. This page does not start a campaign.",
        tabs=_marketing_tabs(),
        active_page="marketing_campaigns",
        read_state="ok",
    )
    view["sources"] = ["LIVE_API: list_campaigns"]
    blocked = _org_block(ctx["organization_id"])
    if blocked is not None:
        view["read_state"] = "unavailable"
        view["alerts"] = [_alert("warning", "Campaigns", blocked["message"])]
        return view
    loaded = _guard("Campaigns", lambda: list_campaigns(organization_id=ctx["organization_id"]))
    if loaded["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"] = [_alert("danger", "Campaigns", loaded["message"])]
        return view
    rows = loaded["value"] or []
    view["table"] = _table(
        "Campaigns",
        [
            {"key": "name", "label": "Campaign", "priority": "high"},
            {"key": "status", "label": "Status", "priority": "high"},
            {"key": "goal", "label": "Goal", "priority": "medium"},
            {"key": "channels", "label": "Channels", "priority": "low"},
        ],
        [
            {
                "name": str(row.get("name") or "—"),
                "status": str(row.get("status") or "—"),
                "goal": str(row.get("goal") or "—"),
                "channels": ", ".join(row.get("channels") or []) or "—",
            }
            for row in rows
        ],
        "No campaigns were returned for this organization.",
    )
    return view


def build_marketing_demand(**ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Growth",
        title="Demand",
        section="demand",
        purpose="Demand intake already governed on the demand workspace.",
        tabs=_marketing_tabs(),
        active_page="marketing_demand",
        read_state="ok",
    )
    demand = _demand(ctx["organization_id"])
    view["sources"] = ["DERIVED_FROM_LIVE_API: build_demand_contacts_snapshot"]
    view["primary_action"] = {"href": "/demand", "label": "Open demand"}
    if demand["state"] != "ok":
        view["read_state"] = demand["state"]
        view["alerts"] = [_alert("warning" if demand["state"] == "unavailable" else "danger", "Demand", demand["message"])]
        return view
    data = demand["value"] or {}
    pending = data.get("pending_demands") or []
    view["kpis"] = [
        _kpi("Pending demand", str(len(pending)), "Handoffs still waiting on intake.", action_href="/demand", action_label="Review", source="DERIVED_FROM_LIVE_API"),
    ]
    view["table"] = _table(
        "Pending demand",
        [
            {"key": "name", "label": "Person", "priority": "high"},
            {"key": "email", "label": "Email", "priority": "medium"},
            {"key": "source", "label": "Source", "priority": "low"},
        ],
        [
            {
                "name": str(row.get("name") or row.get("person_label") or "—"),
                "email": str(row.get("email") or "—"),
                "source": str(row.get("source") or "—"),
            }
            for row in pending
        ],
        "No pending demand was returned.",
    )
    return view


def build_marketing_seo(**ctx: Any) -> dict[str, Any]:
    del ctx
    view = _base(
        workspace="Growth",
        title="SEO",
        section="seo",
        purpose="SEO readiness already has screens. This tab does not invent scores.",
        tabs=_marketing_tabs(),
        active_page="marketing_seo",
        read_state="ok",
    )
    view["sources"] = ["STATIC_PRODUCT_COPY: links to existing SEO screens"]
    view["alerts"] = [
        _alert(
            "info",
            "Scores stay on the SEO screens",
            "Opening this tab does not run a new crawl or substitute a score.",
        )
    ]
    view["primary_action"] = {"href": "/seo", "label": "Open SEO readiness"}
    view["table"] = _table(
        "Existing SEO destinations",
        [
            {"key": "screen", "label": "Screen", "priority": "high"},
            {"key": "note", "label": "What it is", "priority": "high"},
        ],
        [
            {"screen": "SEO readiness", "note": "Existing readiness list", "_href": "/seo"},
            {"screen": "Technical SEO", "note": "Existing technical screen", "_href": "/seo/technical"},
        ],
        "SEO screens are unavailable.",
    )
    return view


def build_marketing_performance(**_ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Growth",
        title="Content Performance",
        section="performance",
        purpose="Tracker and QA counts. Publication performance is unknown.",
        tabs=_marketing_tabs(),
        active_page="marketing_performance",
        read_state="ok",
    )
    loaded = _tracker_items()
    view["sources"] = ["LIVE_API: content studio tracker list", "API_GAP: publication truth"]
    view["gaps"] = [API_GAPS[1], API_GAPS[2]]
    view["alerts"] = [_alert("unknown", "Publication performance unknown", _PUBLICATION_TRUTH_GAP)]
    if loaded["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"].append(_alert("danger", "Tracker", loaded["message"]))
        return view
    items = (loaded["value"] or {}).get("items") or []
    qa_pass = sum(1 for item in items if str(item.get("qa_status") or "").upper() == "PASS")
    view["kpis"] = [
        _kpi("Content rows", str(len(items)), "Tracker inventory. Not a reach metric.", source="LIVE_API"),
        _kpi("QA pass", str(qa_pass), "QA pass is not publication and not performance.", source="DERIVED_FROM_LIVE_API"),
        _kpi("Verified published", "Unknown", "No canonical publication truth on this read.", source="API_GAP"),
    ]
    view["primary_action"] = {"href": "/analytics", "label": "Open analytics"}
    return view


def build_marketing_analytics(**_ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Growth",
        title="Analytics",
        section="analytics",
        purpose="The existing analytics screen remains the chart surface. Tracker status labeled Published there is not verified publication.",
        tabs=_marketing_tabs(),
        active_page="marketing_analytics",
        read_state="ok",
    )
    view["sources"] = ["STATIC_PRODUCT_COPY"]
    view["alerts"] = [
        _alert(
            "unknown",
            "Do not read tracker status as published",
            "Analytics still counts tracker rows whose status is Published. That count is a tracker status, not verified publication.",
        )
    ]
    view["primary_action"] = {"href": "/analytics", "label": "Open analytics"}
    return view


def _content_base(title: str, section: str, purpose: str, active_page: str) -> dict[str, Any]:
    view = _base(
        workspace="Content",
        title=title,
        section=section,
        purpose=purpose,
        tabs=_content_tabs(),
        active_page=active_page,
        read_state="ok",
    )
    view["gaps"] = [API_GAPS[1]]
    view["alerts"] = [_alert("info", "Publication truth is a separate read", _PUBLICATION_TRUTH_GAP)]
    return view


def build_content_overview(**ctx: Any) -> dict[str, Any]:
    view = _content_base(
        "Overview",
        "overview",
        "Inventory, QA, editorial queue, and publishing jobs. None of those counts are verified publication.",
        "os_content",
    )
    loaded = _tracker_items()
    editorial = _guard("Editorial pending", build_editorial_pending)
    jobs = _guard("Publishing jobs", lambda: pe.list_queue(include_terminal=True))
    view["sources"] = [
        "LIVE_API: content studio tracker",
        "LIVE_API: editorial pending",
        "LIVE_API: publishing job queue (orchestration state only)",
    ]
    if loaded["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"].append(_alert("danger", "Tracker", loaded["message"]))
        return view
    items = (loaded["value"] or {}).get("items") or []
    qa_pass = sum(1 for item in items if str(item.get("qa_status") or "").upper() == "PASS")
    pending_count = "—"
    if editorial["state"] == "ok":
        pending_count = str((editorial["value"] or {}).get("count", 0))
    else:
        view["alerts"].append(_alert("danger", "Editorial", editorial["message"]))
    job_count = "—"
    if jobs["state"] == "ok":
        job_count = str(len(jobs["value"] or []))
    else:
        view["alerts"].append(_alert("danger", "Publishing jobs", jobs["message"]))
    view["kpis"] = [
        _kpi("Content rows", str(len(items)), "Tracker inventory.", action_href="/os/content/studio", action_label="Open studio", source="LIVE_API"),
        _kpi("QA pass", str(qa_pass), "QA pass is not publication.", action_href="/os/content/editorial", action_label="Open editorial", source="DERIVED_FROM_LIVE_API"),
        _kpi("Editorial pending", pending_count, "Items not yet editorially approved.", source="LIVE_API"),
        _kpi("Orchestration jobs", job_count, "Publishing jobs. Job existence is not publication.", action_href="/publishing", action_label="Open publishing queue", source="LIVE_API"),
        _kpi("Verified published", "Unknown", "Requires publication truth and verification.", source="API_GAP"),
        _kpi(
            "Active week",
            str(ctx.get("active_week") or "—"),
            "Runtime active week. Not a publication claim.",
            source="LIVE_API",
        ),
    ]
    view["table"] = _content_table(items[:25])
    view["primary_action"] = {"href": "/content-ops", "label": "Open the live week read"}
    view["next_steps"] = [
        {
            "href": "/os/content/pipeline",
            "label": "Pipeline",
            "detail": "Contextual action. Opening it does not start a run or publish.",
        }
    ]
    return view


def build_content_calendar(**_ctx: Any) -> dict[str, Any]:
    view = _content_base(
        "Calendar",
        "calendar",
        "Tracker rows linked to the existing week screens.",
        "os_content_calendar",
    )
    loaded = _tracker_items()
    view["sources"] = ["LIVE_API: content studio tracker"]
    view["primary_action"] = {"href": "/weeks", "label": "Open content calendar"}
    if loaded["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"].append(_alert("danger", "Calendar", loaded["message"]))
        return view
    items = (loaded["value"] or {}).get("items") or []
    rows = _content_rows(items)
    view["table"] = _table(
        "Calendar index",
        [
            {"key": "content_id", "label": "Week", "priority": "high"},
            {"key": "title", "label": "Title", "priority": "high"},
            {"key": "current_step", "label": "Step", "priority": "medium"},
            {"key": "publication_truth", "label": "Publication truth", "priority": "high"},
        ],
        [
            {
                "content_id": row["content_id"],
                "title": row["title"],
                "current_step": row["current_step"],
                "publication_truth": "Not the current-week read",
                "_href": row["calendar_href"],
            }
            for row in rows
        ],
        "No weeks were returned.",
    )
    return view


def build_content_studio(**_ctx: Any) -> dict[str, Any]:
    view = _content_base(
        "Studio",
        "studio",
        "Lifecycle evidence is artifact presence and tracker step. It is not a publication claim.",
        "os_content_studio",
    )
    loaded = _tracker_items()
    view["sources"] = ["LIVE_API: content studio tracker"]
    view["primary_action"] = {"href": "/content-studio", "label": "Open Content Studio"}
    if loaded["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"].append(_alert("danger", "Studio", loaded["message"]))
        return view
    view["table"] = _content_table((loaded["value"] or {}).get("items") or [])
    return view


def build_content_editorial(**_ctx: Any) -> dict[str, Any]:
    view = _content_base(
        "Editorial",
        "editorial",
        "Pending editorial decisions. Editorial ready is not publication authorization.",
        "os_content_editorial",
    )
    loaded = _guard("Editorial pending", build_editorial_pending)
    view["sources"] = ["LIVE_API: build_editorial_pending"]
    view["primary_action"] = {"href": "/editorial", "label": "Open editorial queue"}
    if loaded["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"].append(_alert("danger", "Editorial", loaded["message"]))
        return view
    items = (loaded["value"] or {}).get("items") or []
    view["table"] = _table(
        "Editorial pending",
        [
            {"key": "content_id", "label": "Content", "priority": "high"},
            {"key": "title", "label": "Title", "priority": "high"},
            {"key": "editorial_state", "label": "Editorial state", "priority": "high"},
            {"key": "can_approve", "label": "Server says can approve", "priority": "medium"},
        ],
        [
            {
                "content_id": str(item.get("content_id") or "—"),
                "title": str(item.get("title") or "—"),
                "editorial_state": str(item.get("editorial_state") or "—"),
                "can_approve": "yes" if item.get("can_approve") else "no",
                "_href": str(item.get("ui_path") or ""),
            }
            for item in items
        ],
        "No editorial items are pending.",
    )
    return view


def build_content_publishing(**_ctx: Any) -> dict[str, Any]:
    view = _content_base(
        "Publishing",
        "publishing",
        "Orchestration jobs only. A job state is not canonical publication truth.",
        "os_content_publishing",
    )
    loaded = _guard("Publishing jobs", lambda: pe.list_queue(include_terminal=True))
    view["sources"] = ["LIVE_API: publishing_engine.list_queue"]
    view["primary_action"] = {"href": "/publishing", "label": "Open publishing queue"}
    if loaded["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"].append(_alert("danger", "Publishing", loaded["message"]))
        return view
    jobs = loaded["value"] or []
    view["table"] = _table(
        "Publishing jobs. The truth column stays unknown.",
        [
            {"key": "job_id", "label": "Job", "priority": "high"},
            {"key": "channel", "label": "Channel", "priority": "medium"},
            {"key": "orchestration_state", "label": "Job state", "priority": "high"},
            {"key": "shown_as", "label": "Shown as", "priority": "high"},
            {"key": "publication_truth", "label": "Publication truth", "priority": "high"},
        ],
        [
            {
                "job_id": str(job.get("job_id") or "—"),
                "channel": str(job.get("channel") or "—"),
                "orchestration_state": str(job.get("state") or "unknown"),
                "shown_as": _publication_label(str(job.get("state") or "")),
                "publication_truth": "Not the current-week read",
                "_href": f"/publishing/{job.get('job_id')}" if job.get("job_id") else "",
            }
            for job in jobs
        ],
        "No publishing jobs were returned.",
    )
    return view


def build_content_seo(**_ctx: Any) -> dict[str, Any]:
    view = _content_base(
        "SEO",
        "seo",
        "Tracker rows whose current step mentions SEO. This page does not score the site.",
        "os_content_seo",
    )
    loaded = _tracker_items()
    view["sources"] = ["LIVE_API: content studio tracker", "STATIC_PRODUCT_COPY: link to the existing SEO screen"]
    view["primary_action"] = {"href": "/seo", "label": "Open SEO readiness"}
    view["alerts"].append(
        _alert(
            "unknown",
            "SEO readiness is not scored here",
            "A readiness number is not derived from tracker status. Use the existing SEO screen for the live scan.",
        )
    )
    if loaded["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"].append(_alert("danger", "SEO", loaded["message"]))
        return view
    items = (loaded["value"] or {}).get("items") or []
    seo_rows = [
        item
        for item in items
        if "seo" in str(item.get("current_step") or "").lower()
        or "seo" in str(item.get("status") or "").lower()
    ]
    view["kpis"] = [
        _kpi("Rows on an SEO step", str(len(seo_rows)), "Tracker step text. Not a readiness score.", source="DERIVED_FROM_LIVE_API"),
        _kpi("SEO readiness", "Unknown", "Not invented from the tracker.", source="API_GAP"),
    ]
    view["table"] = _content_table(seo_rows)
    return view


def build_content_pipeline(**_ctx: Any) -> dict[str, Any]:
    view = _content_base(
        "Pipeline",
        "pipeline",
        "How many tracker rows sit on each step. This page does not start a run.",
        "os_content_pipeline",
    )
    loaded = _tracker_items()
    view["sources"] = ["LIVE_API: content studio tracker"]
    view["primary_action"] = {"href": "/pipeline", "label": "Open run pipeline"}
    view["alerts"].append(
        _alert(
            "info",
            "Run controls stay on the pipeline screen",
            "Step counts are tracker positions. They are not publication and this page does not enqueue work.",
        )
    )
    if loaded["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"].append(_alert("danger", "Pipeline", loaded["message"]))
        return view
    items = (loaded["value"] or {}).get("items") or []
    counts: dict[str, int] = {}
    for item in items:
        step = str(item.get("current_step") or "Unknown step")
        counts[step] = counts.get(step, 0) + 1
    view["pipeline"] = [
        {"stage": step, "count": str(count), "value": "Publication truth unknown"}
        for step, count in counts.items()
    ]
    view["kpis"] = [
        _kpi("Tracker rows", str(len(items)), "Inventory currently in the tracker.", source="LIVE_API"),
        _kpi("Verified published", "Unknown", "Step position is not publication.", source="API_GAP"),
    ]
    return view


def build_content_analytics(**_ctx: Any) -> dict[str, Any]:
    view = _content_base(
        "Analytics",
        "analytics",
        "Content counts from the tracker. Reach and verified publication are unknown.",
        "os_content_analytics",
    )
    loaded = _tracker_items()
    view["sources"] = ["LIVE_API: content studio tracker"]
    view["primary_action"] = {"href": "/analytics", "label": "Open analytics"}
    if loaded["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"].append(_alert("danger", "Analytics", loaded["message"]))
        return view
    items = (loaded["value"] or {}).get("items") or []
    view["kpis"] = [
        _kpi("Content rows", str(len(items)), "Inventory count.", source="LIVE_API"),
        _kpi("Verified published", "Unknown", "Not derived from QA or tracker status.", source="API_GAP"),
    ]
    return view


def build_operations_overview(**ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Operations",
        title="Overview",
        section="overview",
        purpose="Approvals and activity that already exist. Automations are withheld until they are tenant-scoped.",
        tabs=_operations_tabs(),
        active_page="operations",
        read_state="ok",
    )
    approvals = _guard(
        "Approvals",
        lambda: build_approvals_snapshot(organization_id=ctx["organization_id"]),
    )
    activity = _guard(
        "Activity",
        lambda: build_activity_snapshot(organization_id=ctx["organization_id"]),
    )
    view["sources"] = [
        "DERIVED_FROM_LIVE_API: build_approvals_snapshot",
        "DERIVED_FROM_LIVE_API: build_activity_snapshot",
    ]
    view["gaps"] = [API_GAPS[3]]
    view["primary_action"] = {"href": "/pending-approvals", "label": "Open approvals"}
    if approvals["state"] == "ok":
        data = approvals["value"] or {}
        if data.get("state") == "unavailable":
            view["alerts"] = [_alert("warning", "Approvals", data.get("message") or "Unavailable")]
        else:
            view["kpis"] = [
                _kpi(
                    "Approvals waiting",
                    str(data.get("pending_count", "—")),
                    "Pending governed requests.",
                    action_href="/pending-approvals",
                    action_label="Review",
                    source="LIVE_API",
                )
            ]
    else:
        view["read_state"] = "error"
        view["alerts"] = [_alert("danger", "Approvals", approvals["message"])]
    if activity["state"] != "ok":
        view["alerts"].append(_alert("danger", "Activity", activity["message"]))
    return view


def build_operations_approvals(**ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Operations",
        title="Approvals",
        section="approvals",
        purpose="Pending approval requests. This page does not approve them.",
        tabs=_operations_tabs(),
        active_page="operations_approvals",
        read_state="ok",
    )
    loaded = _guard(
        "Approvals",
        lambda: build_approvals_snapshot(organization_id=ctx["organization_id"]),
    )
    view["sources"] = ["DERIVED_FROM_LIVE_API: build_approvals_snapshot"]
    view["primary_action"] = {"href": "/pending-approvals", "label": "Open approval inbox"}
    if loaded["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"] = [_alert("danger", "Approvals", loaded["message"])]
        return view
    data = loaded["value"] or {}
    if data.get("state") == "unavailable":
        view["read_state"] = "unavailable"
        view["alerts"] = [_alert("warning", "Approvals", data.get("message") or "Unavailable")]
        return view
    pending = data.get("pending") or []
    view["table"] = _table(
        "Pending approvals",
        [
            {"key": "action_type", "label": "Action", "priority": "high"},
            {"key": "status", "label": "Status", "priority": "high"},
            {"key": "summary", "label": "Summary", "priority": "medium"},
        ],
        [
            {
                "action_type": str(item.get("action_type") or "—"),
                "status": str(item.get("status") or "pending"),
                "summary": str(item.get("summary") or item.get("reason") or item.get("title") or "—"),
            }
            for item in pending
        ],
        "No approvals are pending.",
    )
    return view


def build_operations_activity(**ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Operations",
        title="Activity",
        section="activity",
        purpose="Organization-scoped action log.",
        tabs=_operations_tabs(),
        active_page="operations_activity",
        read_state="ok",
    )
    loaded = _guard(
        "Activity",
        lambda: build_activity_snapshot(organization_id=ctx["organization_id"]),
    )
    view["sources"] = ["DERIVED_FROM_LIVE_API: build_activity_snapshot"]
    view["primary_action"] = {"href": "/activity", "label": "Open activity"}
    if loaded["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"] = [_alert("danger", "Activity", loaded["message"])]
        return view
    data = loaded["value"] or {}
    events = data.get("events") or data.get("actions") or data.get("items") or []
    if data.get("state") == "unavailable":
        view["read_state"] = "unavailable"
        view["alerts"] = [_alert("warning", "Activity", data.get("message") or "Unavailable")]
        return view
    view["table"] = _table(
        "Activity",
        [
            {"key": "action_type", "label": "Action", "priority": "high"},
            {"key": "actor", "label": "Actor", "priority": "medium"},
            {"key": "created_at", "label": "When", "priority": "medium"},
        ],
        [
            {
                "action_type": str(event.get("action_type") or event.get("type") or "—"),
                "actor": str(event.get("actor") or event.get("requested_by") or "—"),
                "created_at": str(event.get("created_at") or event.get("timestamp") or "—"),
            }
            for event in events
            if isinstance(event, dict)
        ],
        "No activity events were returned.",
    )
    return view


def build_operations_automations(**_ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Operations",
        title="Automations",
        section="automations",
        purpose="Workflow records are withheld because the list is not organization-scoped.",
        tabs=_operations_tabs(),
        active_page="operations_automations",
        read_state="gap",
    )
    view["gaps"] = [API_GAPS[3]]
    view["sources"] = ["API_GAP"]
    view["alerts"] = [
        _alert(
            "blocked",
            "Automations not listed",
            "GET /api/v1/automation/workflows exists and is process-global. Rendering it here could cross tenants, so no workflow rows are shown.",
        )
    ]
    view["table"] = _table(
        "Automations",
        [{"key": "name", "label": "Workflow", "priority": "high"}],
        [],
        "No tenant-scoped automation list is available.",
    )
    return view


def build_operations_jobs(**_ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Operations",
        title="Jobs",
        section="jobs",
        purpose="Process scheduler status. This is not a tenant job history and it does not run jobs.",
        tabs=_operations_tabs(),
        active_page="operations_jobs",
        read_state="ok",
    )
    loaded = _guard("Scheduler", scheduler.status)
    view["sources"] = ["LIVE_API: scheduler.status"]
    view["alerts"] = [
        _alert(
            "info",
            "Process scheduler",
            f"Heartbeat enabled flag is {'on' if heartbeat_enabled() else 'off'}. This page cannot start a job.",
        )
    ]
    if loaded["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"].append(_alert("danger", "Scheduler", loaded["message"]))
        return view
    status = loaded["value"] or {}
    jobs = status.get("jobs") or []
    view["kpis"] = [
        _kpi(
            "Scheduler running",
            "yes" if status.get("running") else "no",
            "In-process scheduler task.",
            source="LIVE_API",
        )
    ]
    view["table"] = _table(
        "Registered jobs",
        [
            {"key": "name", "label": "Job", "priority": "high"},
            {"key": "last_status", "label": "Last status", "priority": "high"},
            {"key": "last_run_at", "label": "Last run", "priority": "medium"},
        ],
        [
            {
                "name": str(job.get("name") or "—"),
                "last_status": str(job.get("last_status") or "—"),
                "last_run_at": str(job.get("last_run_at") or "—"),
            }
            for job in jobs
        ],
        "No scheduler jobs are registered in this process.",
    )
    return view


def _agent_registry() -> dict[str, Any]:
    return _guard("Agent registry", AgentCoordinator.list_agents)


def build_agents_hub(**_ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Agents",
        title="Agent Hub",
        section="overview",
        purpose="Registered agent names in this process. Registration is not permission to act.",
        tabs=_agent_tabs(),
        active_page="agents",
        read_state="ok",
    )
    hermes = _hermes_status()
    view["hermes"] = hermes
    loaded = _agent_registry()
    view["sources"] = ["LIVE_API: AgentCoordinator.list_agents", "LIVE_API: Hermes endpoint presence"]
    view["primary_action"] = {"href": "/mcp", "label": "Open MCP hub"}
    if loaded["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"].append(_alert("danger", "Registry", loaded["message"]))
        return view
    agents = loaded["value"] or []
    view["table"] = _table(
        "Agent registry",
        [
            {"key": "name", "label": "Agent", "priority": "high"},
            {"key": "type", "label": "Type", "priority": "medium"},
            {"key": "authority", "label": "Authority from this screen", "priority": "high"},
        ],
        [
            {
                "name": str(agent.get("name") or "—"),
                "type": str(agent.get("type") or agent.get("agent_type") or "—"),
                "authority": "none",
            }
            for agent in agents
        ],
        "No agents are registered in this process.",
    )
    return view


def build_agents_runs(**ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Agents",
        title="Runs",
        section="runs",
        purpose="Organization orchestration counts already used by Command Center. This page does not execute a run.",
        tabs=_agent_tabs(),
        active_page="agents_runs",
        read_state="ok",
    )
    view["sources"] = ["DERIVED_FROM_LIVE_API: command center agent_orchestration"]
    view["hermes"] = _hermes_status()
    blocked = _org_block(ctx["organization_id"])
    if blocked is not None:
        view["read_state"] = "unavailable"
        view["alerts"] = [_alert("warning", "Runs", blocked["message"])]
        return view
    loaded = _guard(
        "Orchestration summary",
        lambda: build_command_center_snapshot(organization_id=ctx["organization_id"]),
    )
    if loaded["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"] = [_alert("danger", "Runs", loaded["message"])]
        return view
    summary = (loaded["value"] or {}).get("agent_orchestration") or {}
    counts = summary.get("counts") or {}
    view["kpis"] = [
        _kpi(key.replace("_", " "), str(value), "Count from the orchestration summary.", source="DERIVED_FROM_LIVE_API")
        for key, value in counts.items()
    ]
    pause = summary.get("pause") or {}
    if pause:
        view["alerts"] = [
            _alert(
                "info",
                "Runtime gates",
                ", ".join(f"{key}={value}" for key, value in pause.items()),
            )
        ]
    return view


def build_agents_capabilities(**_ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Agents",
        title="Capabilities",
        section="capabilities",
        purpose="Capability strings stored on the process registry. They are not enabled actions.",
        tabs=_agent_tabs(),
        active_page="agents_capabilities",
        read_state="ok",
    )
    loaded = _agent_registry()
    view["sources"] = ["LIVE_API: AgentCoordinator.list_agents"]
    if loaded["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"] = [_alert("danger", "Capabilities", loaded["message"])]
        return view
    rows = []
    for agent in loaded["value"] or []:
        capabilities = agent.get("capabilities") or []
        if isinstance(capabilities, str):
            capabilities = [capabilities]
        if not capabilities:
            rows.append(
                {
                    "name": str(agent.get("name") or "—"),
                    "capability": "—",
                    "enabled_here": "no",
                }
            )
            continue
        for capability in capabilities:
            rows.append(
                {
                    "name": str(agent.get("name") or "—"),
                    "capability": str(capability),
                    "enabled_here": "no",
                }
            )
    view["table"] = _table(
        "Declared capabilities",
        [
            {"key": "name", "label": "Agent", "priority": "high"},
            {"key": "capability", "label": "Declared capability", "priority": "high"},
            {"key": "enabled_here", "label": "Enabled by this screen", "priority": "high"},
        ],
        rows,
        "No capabilities were declared.",
    )
    return view


def build_agents_evaluations(**_ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="Agents",
        title="Evaluations",
        section="evaluations",
        purpose="No tenant-scoped evaluation read exists.",
        tabs=_agent_tabs(),
        active_page="agents_evaluations",
        read_state="gap",
    )
    view["gaps"] = [API_GAPS[4]]
    view["sources"] = ["API_GAP"]
    view["alerts"] = [
        _alert(
            "blocked",
            "Evaluations not loaded",
            "Agent performance metrics exist as a process-global API and are not shown, because they are not organization-scoped.",
        )
    ]
    return view


def build_system_overview(**ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="System",
        title="Overview",
        section="overview",
        purpose="Identity, tenant, health, and the gaps that block the rest of the shell.",
        tabs=_system_tabs(),
        active_page="system",
        read_state="ok",
    )
    view["sources"] = ["STATIC_PRODUCT_COPY", "LIVE_API: identity and tenant context already on the request"]
    view["gaps"] = list(API_GAPS)
    identity = ctx.get("identity") or {}
    tenant = ctx.get("tenant") or {}
    view["kpis"] = [
        _kpi(
            "Signed in",
            "yes" if identity.get("is_human") else "no",
            "Human session on this request.",
            source="LIVE_API",
        ),
        _kpi(
            "Workspace",
            str(tenant.get("organization_name") or "Unknown"),
            "Resolved tenant name, or unknown when resolution failed.",
            source="LIVE_API",
        ),
    ]
    view["primary_action"] = {"href": "/os/system/health", "label": "Open system health"}
    return view


def build_system_integrations(**ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="System",
        title="Integrations",
        section="integrations",
        purpose="Connector names and whether a vault entry exists. Secret values are never rendered.",
        tabs=_system_tabs(),
        active_page="system_integrations",
        read_state="ok",
    )
    view["sources"] = ["LIVE_API: connector catalog and vault metadata"]
    gmail_frozen = gmail_beta_frozen()
    view["alerts"] = []
    if gmail_frozen:
        view["alerts"].append(_alert("blocked", "Gmail blocked", gmail_beta_frozen_detail()))
    rows: list[dict[str, str]] = []
    if gmail_frozen:
        rows.append(
            {
                "name": "gmail",
                "label": "Gmail",
                "configured": "blocked",
                "note": "Beta freeze. This screen cannot enable it.",
            }
        )

    def _load() -> list[dict[str, str]]:
        org_id = ctx["organization_id"]
        vaulted = list_configured_connectors(organization_id=org_id) if org_id else {}
        built: list[dict[str, str]] = []
        for connector in CONNECTOR_CATALOG:
            name = str(connector.get("name") or "")
            if name == "gmail":
                continue
            if connector.get("source") == "vault":
                if not org_id:
                    configured = "unknown"
                    note = "Organization context required"
                else:
                    configured = "yes" if name in vaulted else "no"
                    note = "Vault metadata only"
            else:
                env_vars = connector.get("env_vars") or []
                configured = "yes" if env_vars and all(os.environ.get(var) for var in env_vars) else "no"
                note = "Environment presence only"
            built.append(
                {
                    "name": name,
                    "label": str(connector.get("label") or name),
                    "configured": configured,
                    "note": note,
                }
            )
        return built

    loaded = _guard("Connectors", _load)
    if loaded["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"].append(_alert("danger", "Connectors", loaded["message"]))
    else:
        rows.extend(loaded["value"] or [])
    view["table"] = _table(
        "Integrations",
        [
            {"key": "label", "label": "Connector", "priority": "high"},
            {"key": "configured", "label": "Configured", "priority": "high"},
            {"key": "note", "label": "Note", "priority": "medium"},
        ],
        rows,
        "No connectors were returned.",
    )
    return view


def build_system_governance(**_ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="System",
        title="Governance",
        section="governance",
        purpose="Authority stays on the server. This page lists the reads the shell still does not have.",
        tabs=_system_tabs(),
        active_page="system_governance",
        read_state="gap",
    )
    view["gaps"] = list(API_GAPS)
    view["sources"] = ["STATIC_PRODUCT_COPY"]
    view["alerts"] = [
        _alert(
            "info",
            "No policy catalog",
            "Allowed actions on existing screens still come from their server reads. This page does not add any.",
        )
    ]
    view["primary_action"] = {"href": "/pending-approvals", "label": "Open approvals"}
    view["table"] = _table(
        "Known gaps",
        [{"key": "gap", "label": "Gap", "priority": "high"}],
        [{"gap": gap} for gap in API_GAPS],
        "No gaps recorded.",
    )
    return view


def build_system_identity(**ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="System",
        title="Identity",
        section="identity",
        purpose="The human session on this request. API keys are not shown and are not a login.",
        tabs=_system_tabs(),
        active_page="system_identity",
        read_state="ok",
    )
    identity = ctx.get("identity") or {}
    view["sources"] = ["LIVE_API: identity_from_request"]
    if not identity:
        view["read_state"] = "unavailable"
        view["alerts"] = [_alert("warning", "No human session", "Sign in to see identity. Anonymous is not granted actions.")]
        view["primary_action"] = {"href": "/login", "label": "Sign in"}
        return view
    view["table"] = _table(
        "Identity",
        [
            {"key": "field", "label": "Field", "priority": "high"},
            {"key": "value", "label": "Value", "priority": "high"},
        ],
        [
            {"field": "Human", "value": "yes" if identity.get("is_human") else "no"},
            {"field": "Name", "value": str(identity.get("display_name") or "—")},
            {"field": "Role", "value": str(identity.get("role") or "—")},
            {"field": "Auth method", "value": str(identity.get("auth_method") or "—")},
        ],
        "No identity fields were returned.",
    )
    return view


def build_system_tenant(**ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="System",
        title="Tenant",
        section="tenant",
        purpose="Tenant resolved for this request. The shell cannot switch organization by typing an id.",
        tabs=_system_tabs(),
        active_page="system_tenant",
        read_state="ok",
    )
    tenant = ctx.get("tenant") or {}
    view["sources"] = ["LIVE_API: resolve_tenant_context"]
    if not tenant:
        view["read_state"] = "unavailable"
        view["alerts"] = [_alert("warning", "No tenant", "Organization context was not resolved. Tenant-scoped records stay unloaded.")]
        return view
    view["table"] = _table(
        "Tenant",
        [
            {"key": "field", "label": "Field", "priority": "high"},
            {"key": "value", "label": "Value", "priority": "high"},
        ],
        [
            {"field": "Workspace", "value": str(tenant.get("organization_name") or "—")},
            {"field": "Slug", "value": str(tenant.get("organization_slug") or "—")},
            {"field": "Role", "value": str(tenant.get("membership_role") or "—")},
        ],
        "No tenant fields were returned.",
    )
    return view


def build_system_health(**_ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="System",
        title="System Health",
        section="health",
        purpose="Process checks. A failed check stays failed.",
        tabs=_system_tabs(),
        active_page="system_health",
        read_state="ok",
    )
    view["sources"] = ["LIVE_API: database probe", "LIVE_API: scheduler.status", "LIVE_API: Hermes endpoint presence"]

    def _probe() -> str:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
        return "ok"

    database = _guard("Database", _probe)
    sched = _guard("Scheduler", scheduler.status)
    hermes = _hermes_status()
    view["hermes"] = hermes
    view["kpis"] = [
        _kpi(
            "Database",
            "ok" if database["state"] == "ok" else "unavailable",
            "SELECT 1. Failure is not replaced with a healthy reading.",
            source="LIVE_API",
        ),
        _kpi(
            "Scheduler",
            "running" if sched["state"] == "ok" and (sched["value"] or {}).get("running") else "not running",
            "In-process heartbeat task.",
            source="LIVE_API",
        ),
        _kpi("Hermes", hermes["headline"], hermes["detail"], source="LIVE_API"),
    ]
    if database["state"] != "ok":
        view["read_state"] = "error"
        view["alerts"] = [_alert("danger", "Database", database["message"])]
    view["primary_action"] = {"href": "/health", "label": "Open health endpoint"}
    return view


def build_system_settings(**_ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="System",
        title="Settings",
        section="settings",
        purpose="Settings that would change authority, publication, Gmail, or Hermes are not on this page.",
        tabs=_system_tabs(),
        active_page="system_settings",
        read_state="ok",
    )
    view["sources"] = ["STATIC_PRODUCT_COPY"]
    view["alerts"] = [
        _alert(
            "blocked",
            "No authority settings",
            "Internal beta. Remote publication, Gmail, and Hermes execution stay blocked by the server.",
        )
    ]
    view["primary_action"] = {"href": "/login", "label": "Sign in"}
    return view


def build_design_system(**_ctx: Any) -> dict[str, Any]:
    view = _base(
        workspace="System",
        title="Design system",
        section="overview",
        purpose="Token and component specimens for the shell. Specimens are not product records.",
        tabs=[{"id": "overview", "label": "Foundations", "href": "/os/design-system"}],
        active_page="design_system",
        read_state="ok",
    )
    view["design_system"] = True
    view["sources"] = ["STATIC_PRODUCT_COPY"]
    view["alerts"] = [
        _alert(
            "info",
            "In-product specimens",
            "Figma and Lovable were not available in this environment. These specimens are the implemented token set.",
        )
    ]
    return view


_BUILDERS = {
    "home": build_home,
    "revenue_overview": build_revenue_overview,
    "revenue_contacts": build_revenue_contacts,
    "revenue_companies": build_revenue_companies,
    "revenue_deals": build_revenue_deals,
    "revenue_pipeline": build_revenue_pipeline,
    "revenue_activities": build_revenue_activities,
    "revenue_prospecting": build_revenue_prospecting,
    "revenue_intelligence": build_revenue_intelligence,
    "marketing_overview": build_marketing_overview,
    "marketing_campaigns": build_marketing_campaigns,
    "marketing_demand": build_marketing_demand,
    "marketing_seo": build_marketing_seo,
    "marketing_performance": build_marketing_performance,
    "marketing_analytics": build_marketing_analytics,
    "content_overview": build_content_overview,
    "content_calendar": build_content_calendar,
    "content_studio": build_content_studio,
    "content_editorial": build_content_editorial,
    "content_publishing": build_content_publishing,
    "content_seo": build_content_seo,
    "content_pipeline": build_content_pipeline,
    "content_analytics": build_content_analytics,
    "operations_overview": build_operations_overview,
    "operations_approvals": build_operations_approvals,
    "operations_activity": build_operations_activity,
    "operations_automations": build_operations_automations,
    "operations_jobs": build_operations_jobs,
    "agents_hub": build_agents_hub,
    "agents_runs": build_agents_runs,
    "agents_capabilities": build_agents_capabilities,
    "agents_evaluations": build_agents_evaluations,
    "system_overview": build_system_overview,
    "system_integrations": build_system_integrations,
    "system_governance": build_system_governance,
    "system_identity": build_system_identity,
    "system_tenant": build_system_tenant,
    "system_health": build_system_health,
    "system_settings": build_system_settings,
    "design_system": build_design_system,
}
