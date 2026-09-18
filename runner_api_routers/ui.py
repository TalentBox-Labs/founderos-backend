"""UI page routes and templates."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from runner_api_routers.content_studio import (
    ARTIFACT_FILES,
    build_content_detail,
    build_content_list,
)
from runner_api_routers.editorial import (
    build_editorial_item,
    build_editorial_pending,
)
from runner_api_routers.marketing import (
    _marketing_integration_status,
    _marketing_runs,
)
from revenue_os.services.go_to_market_orchestrator import (
    load_recent_orchestration_runs,
)
from revenue_os.services.orchestration_runtime import backend_status
from src.tools import publishing_engine as pe
from src.tools.seo_engine import analyze_technical_site
from runner_api_routers.utils import (
    _last_run_summary,
    _load_runtime,
    _read_tracker,
    _validate_week_id,
    _week_artifacts,
    PROJECT_ROOT,
)
from revenue_os.services.cockpit_read_model import build_cockpit_snapshot
from revenue_os.services.founder_ui_read_model import (
    attach_safe_booking,
    build_activity_snapshot,
    build_approvals_snapshot,
    build_command_center_snapshot,
    build_contact_workspace_snapshot,
    build_demand_contacts_snapshot,
)
from revenue_os.services.operator_flow_read_model import build_operator_flow_snapshot
from revenue_os.services.tenant_resolution import resolve_tenant_context
from revenue_os.services.qualified_demand_service import SOURCE_TO_CONTACT
from runner_api_routers.cockpit import cockpit_operator_status
from runner_api_routers.identity import founder_login_redirect, identity_from_request

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ui"])


def _identity_template_dict(request: Request) -> dict[str, Any] | None:
    ctx = identity_from_request(request)
    return ctx.as_public_dict() if ctx is not None else None


def _tenant_template_dict(request: Request) -> dict[str, Any] | None:
    try:
        tenant = resolve_tenant_context(request)
    except HTTPException:
        return None
    if tenant is None:
        return None
    return {
        "organization_id": tenant.organization_id,
        "organization_name": tenant.organization_name,
        "organization_slug": tenant.organization_slug,
        "membership_role": tenant.membership_role,
    }


def _founder_page_context(request: Request, *, active_page: str) -> dict[str, Any]:
    runtime = _load_runtime()
    tenant_ctx = None
    org_id = None
    try:
        tenant = resolve_tenant_context(request)
        if tenant is not None:
            tenant_ctx = _tenant_template_dict(request)
            org_id = tenant.organization_id
    except HTTPException:
        tenant_ctx = None
        org_id = None
    return {
        "request": request,
        "active_page": active_page,
        "active_week": runtime.get("active_week", "—"),
        "operator": cockpit_operator_status(),
        "identity": _identity_template_dict(request),
        "tenant": tenant_ctx,
        "org_id": org_id,
    }

# Setup templates
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _week_pipeline_steps(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive per-step state from tracker row and artifact existence."""
    wid = row.get("content_id", "")
    arts = _week_artifacts(wid)
    current = (row.get("current_step") or "").lower()
    steps = []
    mapping = [
        ("Brief", "brief"),
        ("SEO", "seo"),
        ("Research", "research"),
        ("Draft", "draft"),
        ("Final", "final"),
        ("Checklist", "checklist"),
    ]
    for label, key in mapping:
        if arts.get(key):
            state = "done"
        elif label.lower() in current:
            state = "active"
        else:
            state = "pending"
        steps.append({"label": label, "state": state})
    return steps


def _enrich_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enrich tracker rows with artifact and pipeline state information."""
    enriched = []
    for r in rows:
        wid = r.get("content_id", "")
        arts = _week_artifacts(wid)
        enriched.append(
            {
                **r,
                "has_draft": arts.get("draft", False),
                "has_final": arts.get("final", False),
                "has_qa": (
                    PROJECT_ROOT / "output" / "qa_reports" / f"{wid}_Draft_Validation.md"
                ).is_file(),
                "has_checklist": arts.get("checklist", False),
                "pipeline_steps": _week_pipeline_steps(r),
            }
        )
    return enriched


def _validator_results(week_id: str) -> list[tuple[str, str]]:
    """Read QA report verdicts for the week detail template."""
    qa_dir = PROJECT_ROOT / "output" / "qa_reports"
    suffix_map = {
        "research_mapper": f"{week_id}_Research_Map.md",
        "draft_validator": f"{week_id}_Draft_Validation.md",
        "structure_checker": f"{week_id}_Structure_Check.md",
        "metadata_checker": f"{week_id}_Metadata_Check.md",
        "publish_checklist_checker": f"{week_id}_Publish_Checklist_Check.md",
    }
    results: list[tuple[str, str]] = []
    for name, fname in suffix_map.items():
        path = qa_dir / fname
        if not path.is_file():
            results.append((name, "—"))
            continue
        text = path.read_text(encoding="utf-8")
        verdict = "—"
        for line in reversed(text.splitlines()):
            if "PASS" in line:
                verdict = "PASS"
                break
            if "FAIL" in line:
                verdict = "FAIL"
                break
        results.append((name, verdict))
    return results


def _dashboard_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate dashboard statistics from tracker rows."""
    total = len(rows)
    qa_passed = sum(1 for r in rows if (r.get("qa_status") or "").upper() == "PASS")
    published = sum(
        1
        for r in rows
        if "publish" in (r.get("status") or "").lower()
        or "complete" in (r.get("status") or "").lower()
    )
    in_prog = sum(
        1
        for r in rows
        if "progress" in (r.get("status") or "").lower()
        or "review" in (r.get("status") or "").lower()
    )
    weeks_set = {r.get("content_id", "") for r in rows}
    return {
        "total": total,
        "weeks": len(weeks_set),
        "qa_passed": qa_passed,
        "qa_pct": round(qa_passed / total * 100) if total else 0,
        "published": published,
        "in_progress": in_prog,
    }


@router.get("/", response_class=HTMLResponse)
def page_dashboard(request: Request) -> HTMLResponse:
    """Dashboard home page."""
    logger.info("Loading dashboard page")
    rows = _read_tracker()
    enriched = _enrich_rows(rows)
    runtime = _load_runtime()
    last_run = _last_run_summary()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "request": request,
            "active_page": "dashboard",
            "active_week": runtime.get("active_week", "—"),
            "stats": _dashboard_stats(enriched),
            "weeks": enriched,
            "last_run": last_run,
        },
    )


@router.get("/weeks", response_class=HTMLResponse)
def page_weeks(request: Request) -> HTMLResponse:
    """Content calendar page."""
    logger.info("Loading weeks page")
    rows = _read_tracker()
    enriched = _enrich_rows(rows)
    runtime = _load_runtime()

    return templates.TemplateResponse(
        request=request,
        name="weeks.html",
        context={
            "request": request,
            "active_page": "weeks",
            "active_week": runtime.get("active_week", "—"),
            "weeks": enriched,
        },
    )


@router.get("/weeks/{week_id}", response_class=HTMLResponse)
def page_week_detail(week_id: str, request: Request) -> HTMLResponse:
    """Week detail page."""
    try:
        _validate_week_id(week_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("Loading week detail", extra={"week_id": week_id})

    rows = _read_tracker()
    row = next((r for r in rows if r.get("content_id") == week_id), None)
    if not row:
        raise HTTPException(status_code=404, detail="Week not found")

    enriched_rows = _enrich_rows([row])
    enriched = enriched_rows[0] if enriched_rows else row

    runtime = _load_runtime()
    profile_path = PROJECT_ROOT / "data" / "week_runtime" / f"{week_id}.json"
    if profile_path.is_file():
        runtime_json = json.dumps(
            json.loads(profile_path.read_text(encoding="utf-8")), indent=2
        )
    else:
        runtime_json = json.dumps(runtime, indent=2)

    return templates.TemplateResponse(
        request=request,
        name="week_detail.html",
        context={
            "request": request,
            "active_page": "week_detail",
            "active_week": runtime.get("active_week", "—"),
            # Template binds tracker fields as `row.*`; keep `week` alias too.
            "row": enriched,
            "week": enriched,
            "week_id": week_id,
            "artifacts": _week_artifacts(week_id),
            "validators": _validator_results(week_id),
            "qa_report_html": None,
            "runtime_json": runtime_json,
        },
    )


@router.get("/weeks/{week_id}/file/{filename:path}", response_class=HTMLResponse)
def page_file_view(
    week_id: str, filename: str, request: Request
) -> HTMLResponse:
    """File viewer page."""
    try:
        _validate_week_id(week_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not any(r.get("content_id") == week_id for r in _read_tracker()):
        raise HTTPException(status_code=404, detail="Week not found")
    logger.info("Loading file view", extra={"week_id": week_id, "filename": filename})

    file_path = PROJECT_ROOT / "input" / week_id / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    content = file_path.read_text(encoding="utf-8")
    runtime = _load_runtime()

    return templates.TemplateResponse(
        request=request,
        name="file_view.html",
        context={
            "request": request,
            "active_page": "file_view",
            "active_week": runtime.get("active_week", "—"),
            "week_id": week_id,
            "filename": filename,
            "content": content,
            "char_count": len(content),
            "line_count": content.count("\n") + 1,
        },
    )


@router.get("/content-studio", response_class=HTMLResponse)
def page_content_studio(request: Request) -> HTMLResponse:
    """Content Studio list — data from Content Studio API builders only."""
    logger.info("Loading Content Studio list page")
    runtime = _load_runtime()
    api_error: str | None = None
    items: list[dict[str, Any]] = []
    try:
        payload = build_content_list()
        if not payload.get("ok"):
            api_error = "Content Studio API returned ok=false"
        else:
            items = list(payload.get("items") or [])
    except Exception as exc:  # noqa: BLE001 — surface explicit UI error state
        logger.exception("Content Studio list API failed")
        api_error = str(exc) or "Content Studio API failure"

    statuses = sorted(
        {
            str(i.get("status") or "").strip()
            for i in items
            if (i.get("status") or "").strip()
        }
    )
    week_ids = sorted(
        {
            str(i.get("content_id") or "").strip()
            for i in items
            if (i.get("content_id") or "").strip()
        }
    )

    return templates.TemplateResponse(
        request=request,
        name="content_studio.html",
        context={
            "request": request,
            "active_page": "content_studio",
            "active_week": runtime.get("active_week", "—"),
            "items": items,
            "statuses": statuses,
            "week_ids": week_ids,
            "api_error": api_error,
            "artifact_files": ARTIFACT_FILES,
        },
    )


@router.get("/content-studio/kanban", response_class=HTMLResponse)
def page_content_studio_kanban(request: Request) -> HTMLResponse:
    """Content Studio read-only Kanban — columns = exact API status strings."""
    logger.info("Loading Content Studio Kanban page")
    runtime = _load_runtime()
    api_error: str | None = None
    items: list[dict[str, Any]] = []
    try:
        payload = build_content_list()
        if not payload.get("ok"):
            api_error = "Content Studio API returned ok=false"
        else:
            items = list(payload.get("items") or [])
    except Exception as exc:  # noqa: BLE001 — surface explicit UI error state
        logger.exception("Content Studio Kanban API failed")
        api_error = str(exc) or "Content Studio API failure"

    # Columns = distinct status values as returned by the API (no invented taxonomy).
    columns: list[str] = []
    columns_map: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        raw = item.get("status")
        status = "" if raw is None else str(raw)
        if status not in columns_map:
            columns_map[status] = []
            columns.append(status)
        columns_map[status].append(item)
    # Stable presentation order: non-empty statuses sorted, blank column last if present.
    non_empty = sorted(s for s in columns if s.strip())
    columns = non_empty + ([""] if "" in columns_map else [])

    statuses = sorted({s for s in columns if s.strip()})
    week_ids = sorted(
        {
            str(i.get("content_id") or "").strip()
            for i in items
            if (i.get("content_id") or "").strip()
        }
    )

    return templates.TemplateResponse(
        request=request,
        name="content_studio_kanban.html",
        context={
            "request": request,
            "active_page": "content_studio_kanban",
            "active_week": runtime.get("active_week", "—"),
            "items": items,
            "columns": columns,
            "columns_map": columns_map,
            "statuses": statuses,
            "week_ids": week_ids,
            "api_error": api_error,
        },
    )


@router.get("/content-studio/{content_id}", response_class=HTMLResponse)
def page_content_studio_detail(content_id: str, request: Request) -> HTMLResponse:
    """Content Studio detail — data from Content Studio API builders only."""
    try:
        _validate_week_id(content_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info("Loading Content Studio detail page", extra={"content_id": content_id})
    runtime = _load_runtime()
    api_error: str | None = None
    not_found = False
    item: dict[str, Any] | None = None
    try:
        payload = build_content_detail(content_id)
        item = payload.get("item")
    except HTTPException as exc:
        if exc.status_code == 404:
            not_found = True
        elif exc.status_code == 400:
            raise
        else:
            api_error = str(exc.detail)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Content Studio detail API failed")
        api_error = str(exc) or "Content Studio API failure"

    prev_id: str | None = None
    next_id: str | None = None
    try:
        ids = [
            str(i.get("content_id") or "").strip()
            for i in (build_content_list().get("items") or [])
            if (i.get("content_id") or "").strip()
        ]
        if content_id in ids:
            idx = ids.index(content_id)
            if idx > 0:
                prev_id = ids[idx - 1]
            if idx + 1 < len(ids):
                next_id = ids[idx + 1]
    except Exception:  # noqa: BLE001 — navigation optional if list fails
        logger.warning("Content Studio week navigation unavailable", exc_info=True)

    available_files: list[tuple[str, str]] = []
    if item and isinstance(item.get("artifacts"), dict):
        for key, present in item["artifacts"].items():
            if present and key in ARTIFACT_FILES:
                available_files.append((key, ARTIFACT_FILES[key]))

    return templates.TemplateResponse(
        request=request,
        name="content_studio_detail.html",
        context={
            "request": request,
            "active_page": "content_studio",
            "active_week": runtime.get("active_week", "—"),
            "content_id": content_id,
            "item": item,
            "not_found": not_found,
            "api_error": api_error,
            "prev_id": prev_id,
            "next_id": next_id,
            "available_files": available_files,
            "artifact_files": ARTIFACT_FILES,
        },
    )


@router.get("/editorial", response_class=HTMLResponse)
@router.get("/editorial/pending", response_class=HTMLResponse)
def page_editorial_pending(request: Request) -> HTMLResponse:
    """Editorial Approval pending queue — existing artifacts + decision audit only."""
    logger.info("Loading Editorial Approval pending page")
    runtime = _load_runtime()
    api_error: str | None = None
    items: list[dict[str, Any]] = []
    try:
        payload = build_editorial_pending()
        if not payload.get("ok"):
            api_error = "Editorial pending API returned ok=false"
        else:
            items = list(payload.get("items") or [])
    except Exception as exc:  # noqa: BLE001
        logger.exception("Editorial pending failed")
        api_error = str(exc) or "Editorial pending failure"

    return templates.TemplateResponse(
        request=request,
        name="editorial_pending.html",
        context={
            "request": request,
            "active_page": "editorial",
            "active_week": runtime.get("active_week", "—"),
            "items": items,
            "api_error": api_error,
        },
    )


@router.get("/editorial/{content_id}", response_class=HTMLResponse)
def page_editorial_detail(content_id: str, request: Request) -> HTMLResponse:
    """Editorial Approval detail — decision buttons; reads existing artifacts only."""
    try:
        _validate_week_id(content_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info("Loading Editorial Approval detail", extra={"content_id": content_id})
    runtime = _load_runtime()
    api_error: str | None = None
    not_found = False
    item: dict[str, Any] | None = None
    try:
        payload = build_editorial_item(content_id)
        item = payload
    except HTTPException as exc:
        if exc.status_code == 404:
            not_found = True
        elif exc.status_code == 400:
            raise
        else:
            api_error = str(exc.detail)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Editorial detail failed")
        api_error = str(exc) or "Editorial detail failure"

    return templates.TemplateResponse(
        request=request,
        name="editorial_detail.html",
        context={
            "request": request,
            "active_page": "editorial",
            "active_week": runtime.get("active_week", "—"),
            "content_id": content_id.upper(),
            "item": item,
            "not_found": not_found,
            "api_error": api_error,
        },
    )


@router.get("/publishing", response_class=HTMLResponse)
def page_publishing_queue(request: Request) -> HTMLResponse:
    """Publishing Engine queue — orchestration only (Architecture v2.1)."""
    logger.info("Loading Publishing queue page")
    runtime = _load_runtime()
    api_error: str | None = None
    items: list[dict[str, Any]] = []
    try:
        items = pe.list_queue(include_terminal=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Publishing queue failed")
        api_error = str(exc) or "Publishing queue failure"

    return templates.TemplateResponse(
        request=request,
        name="publishing_queue.html",
        context={
            "request": request,
            "active_page": "publishing",
            "active_week": runtime.get("active_week", "—"),
            "items": items,
            "channels": pe.list_channels(),
            "api_error": api_error,
        },
    )


@router.get("/publishing/{job_id}", response_class=HTMLResponse)
def page_publishing_detail(job_id: str, request: Request) -> HTMLResponse:
    """Publishing job detail — manual publish / retry / cancel."""
    logger.info("Loading Publishing detail", extra={"job_id": job_id})
    runtime = _load_runtime()
    api_error: str | None = None
    not_found = False
    job: dict[str, Any] | None = None
    audit: list[dict[str, Any]] = []
    try:
        job = pe.get_job(job_id)
        if job is None:
            not_found = True
        else:
            audit = pe.load_audit_for_job(job_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Publishing detail failed")
        api_error = str(exc) or "Publishing detail failure"

    return templates.TemplateResponse(
        request=request,
        name="publishing_detail.html",
        context={
            "request": request,
            "active_page": "publishing",
            "active_week": runtime.get("active_week", "—"),
            "job_id": job_id,
            "job": job,
            "audit": audit,
            "not_found": not_found,
            "api_error": api_error,
        },
    )


@router.get("/pipeline", response_class=HTMLResponse)
def page_pipeline(request: Request) -> HTMLResponse:
    """Pipeline trigger UI page."""
    logger.info("Loading pipeline page")
    rows = _read_tracker()
    runtime = _load_runtime()
    active_week = runtime.get("active_week", "—")

    return templates.TemplateResponse(
        request=request,
        name="pipeline.html",
        context={
            "request": request,
            "active_page": "pipeline",
            "active_week": active_week,
            "weeks": rows,
            "last_run": _last_run_summary(),
        },
    )


@router.get("/mcp", response_class=HTMLResponse)
def page_mcp(request: Request) -> HTMLResponse:
    """MCP hub configuration page."""
    logger.info("Loading MCP page")
    runtime = _load_runtime()

    return templates.TemplateResponse(
        request=request,
        name="mcp.html",
        context={
            "request": request,
            "active_page": "mcp",
            "active_week": runtime.get("active_week", "—"),
        },
    )


@router.get("/marketing", response_class=HTMLResponse)
def page_marketing(request: Request) -> HTMLResponse:
    """Marketing agent UI page."""
    logger.info("Loading marketing page")
    runtime = _load_runtime()

    return templates.TemplateResponse(
        request=request,
        name="marketing.html",
        context={
            "request": request,
            "active_page": "marketing",
            "active_week": runtime.get("active_week", "—"),
            "runs": _marketing_runs(),
            "integration_status": _marketing_integration_status(),
            "orchestration_runs": load_recent_orchestration_runs(limit=20),
            "orchestration_status": backend_status(),
        },
    )


@router.get("/sales", response_class=HTMLResponse)
def page_sales(request: Request) -> HTMLResponse:
    """Sales prospecting UI page."""
    logger.info("Loading sales page")
    runtime = _load_runtime()

    return templates.TemplateResponse(
        request=request,
        name="sales.html",
        context={
            "request": request,
            "active_page": "sales",
            "active_week": runtime.get("active_week", "—"),
        },
    )


@router.get("/operator", response_class=HTMLResponse, response_model=None)
def page_operator(request: Request) -> HTMLResponse | RedirectResponse:
    """OF1 — Founder operator workflow (compose frozen commercial APIs)."""
    redirected = founder_login_redirect(request)
    if redirected is not None:
        return redirected
    logger.info("Loading Operator Flow")
    runtime = _load_runtime()
    tenant = resolve_tenant_context(request)
    org_id = tenant.organization_id if tenant else None

    return templates.TemplateResponse(
        request=request,
        name="operator.html",
        context={
            "request": request,
            "active_page": "operator",
            "active_week": runtime.get("active_week", "—"),
            "flow": build_operator_flow_snapshot(organization_id=org_id),
            "operator": cockpit_operator_status(),
            "identity": _identity_template_dict(request),
        },
    )


@router.get("/operator/demand/register", response_class=HTMLResponse, response_model=None)
def page_manual_demand_register(request: Request) -> HTMLResponse | RedirectResponse:
    """MDG1 — Manual Founder Demand Registration (trusted human → MC04.5)."""
    redirected = founder_login_redirect(request)
    if redirected is not None:
        return redirected
    logger.info("Loading Manual Demand Registration")
    runtime = _load_runtime()
    return templates.TemplateResponse(
        request=request,
        name="operator_demand_register.html",
        context={
            "request": request,
            "active_page": "operator",
            "active_week": runtime.get("active_week", "—"),
            "operator": cockpit_operator_status(),
            "allowed_sources": sorted(SOURCE_TO_CONTACT.keys()),
            "identity": _identity_template_dict(request),
        },
    )


@router.get("/cockpit", response_class=HTMLResponse, response_model=None)
def page_cockpit(request: Request) -> HTMLResponse | RedirectResponse:
    """Executive Cockpit — read-model composition + bounded human actions (UI2)."""
    redirected = founder_login_redirect(request)
    if redirected is not None:
        return redirected
    logger.info("Loading Executive Cockpit")
    runtime = _load_runtime()
    tenant = resolve_tenant_context(request)
    org_id = tenant.organization_id if tenant else None
    snapshot = build_cockpit_snapshot(organization_id=org_id)
    operator = cockpit_operator_status()

    return templates.TemplateResponse(
        request=request,
        name="cockpit.html",
        context={
            "request": request,
            "active_page": "cockpit",
            "active_week": runtime.get("active_week", "—"),
            "snapshot": snapshot,
            "operator": operator,
            "identity": _identity_template_dict(request),
            "tenant": _tenant_template_dict(request),
        },
    )


@router.get("/command", response_class=HTMLResponse, response_model=None)
def page_founder_command(request: Request) -> HTMLResponse | RedirectResponse:
    """UI-D1 — Founder Command Center."""
    redirected = founder_login_redirect(request)
    if redirected is not None:
        return redirected
    ctx = _founder_page_context(request, active_page="command")
    ctx["snapshot"] = build_command_center_snapshot(organization_id=ctx.get("org_id"))
    return templates.TemplateResponse(request=request, name="founder_command.html", context=ctx)


@router.get("/demand", response_class=HTMLResponse, response_model=None)
def page_founder_demand(request: Request) -> HTMLResponse | RedirectResponse:
    """UI-D1 — Demand and contacts."""
    redirected = founder_login_redirect(request)
    if redirected is not None:
        return redirected
    ctx = _founder_page_context(request, active_page="demand")
    ctx["snapshot"] = build_demand_contacts_snapshot(organization_id=ctx.get("org_id"))
    return templates.TemplateResponse(request=request, name="founder_demand.html", context=ctx)


@router.get("/contacts/{contact_id}", response_class=HTMLResponse, response_model=None)
def page_founder_contact(
    request: Request, contact_id: str
) -> HTMLResponse | RedirectResponse:
    """UI-D1 — Contact revenue workspace (includes reply / next action)."""
    redirected = founder_login_redirect(request)
    if redirected is not None:
        return redirected
    ctx = _founder_page_context(request, active_page="demand")
    ctx["workspace"] = attach_safe_booking(
        build_contact_workspace_snapshot(
            organization_id=ctx.get("org_id"),
            contact_id=contact_id,
        )
    )
    return templates.TemplateResponse(request=request, name="founder_contact.html", context=ctx)


@router.get("/pending-approvals", response_class=HTMLResponse, response_model=None)
def page_founder_approvals(request: Request) -> HTMLResponse | RedirectResponse:
    """UI-D1 — Governed approval inbox."""
    redirected = founder_login_redirect(request)
    if redirected is not None:
        return redirected
    ctx = _founder_page_context(request, active_page="approvals")
    ctx["snapshot"] = build_approvals_snapshot(organization_id=ctx.get("org_id"))
    return templates.TemplateResponse(request=request, name="founder_approvals.html", context=ctx)


@router.get("/activity", response_class=HTMLResponse, response_model=None)
def page_founder_activity(request: Request) -> HTMLResponse | RedirectResponse:
    """UI-D1 — Activity and provenance."""
    redirected = founder_login_redirect(request)
    if redirected is not None:
        return redirected
    ctx = _founder_page_context(request, active_page="activity")
    ctx["snapshot"] = build_activity_snapshot(organization_id=ctx.get("org_id"))
    return templates.TemplateResponse(request=request, name="founder_activity.html", context=ctx)


@router.get("/analytics", response_class=HTMLResponse)
def page_analytics(request: Request) -> HTMLResponse:
    """Analytics dashboard page."""
    logger.info("Loading analytics page")
    runtime = _load_runtime()

    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={
            "request": request,
            "active_page": "analytics",
            "active_week": runtime.get("active_week", "—"),
        },
    )


@router.get("/health")
def health() -> dict[str, str]:
    """Process liveness only — does NOT verify database connectivity.

    Load balancers / Render healthCheckPath should use this endpoint.
    Database readiness is exposed separately at GET /health/ready.
    """
    return {
        "status": "ok",
        "service": "WorkCrew CMS OS",
        "check": "liveness",
        "database": "not_checked",
    }


@router.get("/seo", response_class=HTMLResponse)
def page_seo_readiness(request: Request) -> HTMLResponse:
    """SEO Readiness Engine — read-only site view (S1)."""
    logger.info("Loading SEO readiness page")
    runtime = _load_runtime()
    api_error: str | None = None
    site: dict[str, Any] | None = None
    try:
        from src.tools.seo_engine import analyze_site

        site = analyze_site().to_dict()
    except Exception as exc:  # noqa: BLE001
        logger.exception("SEO readiness site scan failed")
        api_error = str(exc) or "SEO readiness failure"

    return templates.TemplateResponse(
        request=request,
        name="seo_readiness.html",
        context={
            "request": request,
            "active_page": "seo",
            "active_week": runtime.get("active_week", "—"),
            "site": site,
            "api_error": api_error,
        },
    )


@router.get("/seo/technical", response_class=HTMLResponse)
def page_seo_technical(request: Request) -> HTMLResponse:
    """Technical SEO Engine — read-only site view (S2 additive)."""
    logger.info("Loading Technical SEO page")
    runtime = _load_runtime()
    api_error: str | None = None
    report: dict[str, Any] | None = None
    try:
        report = analyze_technical_site().to_dict()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Technical SEO site scan failed")
        api_error = str(exc) or "Technical SEO failure"

    return templates.TemplateResponse(
        request=request,
        name="seo_technical.html",
        context={
            "request": request,
            "active_page": "seo_technical",
            "active_week": runtime.get("active_week", "—"),
            "report": report,
            "api_error": api_error,
        },
    )


@router.get("/seo/{slug}", response_class=HTMLResponse)
def page_seo_readiness_detail(slug: str, request: Request) -> HTMLResponse:
    """SEO Readiness Engine — read-only page detail (S1)."""
    logger.info("Loading SEO readiness detail", extra={"slug": slug})
    runtime = _load_runtime()
    api_error: str | None = None
    not_found = False
    page: dict[str, Any] | None = None
    try:
        from src.tools.seo_engine import analyze_page_artifact, default_artifact_root as _root

        result = analyze_page_artifact(_root(), slug)
        if any(c.id == "html_missing" for c in result.checks):
            not_found = True
        else:
            page = result.to_dict()
    except Exception as exc:  # noqa: BLE001
        logger.exception("SEO readiness detail failed")
        api_error = str(exc) or "SEO readiness detail failure"

    return templates.TemplateResponse(
        request=request,
        name="seo_readiness_detail.html",
        context={
            "request": request,
            "active_page": "seo",
            "active_week": runtime.get("active_week", "—"),
            "slug": slug,
            "page": page,
            "not_found": not_found,
            "api_error": api_error,
        },
    )


@router.get("/health/debug")
def health_debug() -> dict[str, str]:
    """Process diagnostics (not for load-balancer probes)."""
    return {
        "status": "ok",
        "service": "WorkCrew CMS OS",
        "check": "diagnostics",
        "database": "not_checked",
        "project_root": str(PROJECT_ROOT),
        "python": sys.executable,
    }


@router.get("/health/ready")
def health_ready() -> JSONResponse:
    """Database readiness probe — fails closed without disclosing credentials."""
    from sqlalchemy import text

    from revenue_os.database import SessionLocal
    from revenue_os.db_url import sanitize_exception_for_log

    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
    except Exception as exc:
        # Do not use logger.exception / exc_info — driver errors may embed DSN secrets.
        logger.error(
            "Database readiness check failed (type=%s)",
            sanitize_exception_for_log(exc),
        )
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "service": "WorkCrew CMS OS",
                "check": "readiness",
                "database": "unavailable",
            },
        )
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "service": "WorkCrew CMS OS",
            "check": "readiness",
            "database": "ok",
        },
    )


@router.get("/api/v1/health")
def api_health() -> dict[str, str]:
    """Canonical API liveness check (does not verify the database)."""
    return {
        "status": "ok",
        "service": "WorkCrew CMS OS API",
        "check": "liveness",
        "database": "not_checked",
    }