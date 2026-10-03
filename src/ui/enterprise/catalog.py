"""Information architecture for the enterprise shell.

Routes here are navigation targets that already exist, or new read-only
compositions of those reads. Missing reads are API_GAP entries. Nothing in
this module is fixture data.
"""

from __future__ import annotations

# Runner UI does not mount a tenant-scoped companies collection.
# revenue_os /companies is a different app and is not organization-filtered.
API_GAPS: tuple[str, ...] = (
    "API_GAP: no tenant-scoped companies list on the Founder OS UI app (revenue_os GET /companies is not mounted here and is not organization-filtered)",
    "API_GAP: tracker rows and publishing jobs are not the canonical current-week publication-truth read",
    "API_GAP: no joined evidence record for content → campaign → demand → contact → opportunity → revenue",
    "API_GAP: no tenant-scoped automation workflow list (GET /api/v1/automation/workflows is process-global)",
    "API_GAP: no tenant-scoped agent evaluation read",
    "API_GAP: no notification feed",
    "API_GAP: no global record search API",
    "API_GAP: no tenant-scoped revenue intelligence rollup (revenue_os.services.revenue_intelligence.get_dashboard_stats is unscoped and is not used)",
    "API_GAP: saved views are not implemented",
    "API_GAP: governance policy catalog has no read endpoint",
)

BACKEND_DEPENDENCIES: tuple[str, ...] = (
    "BACKEND_DEPENDENCY_REQUIRED: none for the current-week publication-truth reader; the live reader is already the default",
    "BACKEND_DEPENDENCY_REQUIRED: tenant-scoped companies list before a Companies table can render records",
    "BACKEND_DEPENDENCY_REQUIRED: tenant-scoped relationship graph before cross-workspace funnel claims",
)


def resolve_surface(workspace: str, section: str) -> str | None:
    """Map /os/{workspace}/{section} to a surface id. Unknown paths are absent."""
    key = (workspace, section)
    return _ROUTES.get(key)


_ROUTES: dict[tuple[str, str], str] = {
    ("revenue", "overview"): "revenue_overview",
    ("revenue", "contacts"): "revenue_contacts",
    ("revenue", "companies"): "revenue_companies",
    ("revenue", "deals"): "revenue_deals",
    ("revenue", "pipeline"): "revenue_pipeline",
    ("revenue", "activities"): "revenue_activities",
    ("revenue", "prospecting"): "revenue_prospecting",
    ("revenue", "intelligence"): "revenue_intelligence",
    ("marketing", "overview"): "marketing_overview",
    ("marketing", "campaigns"): "marketing_campaigns",
    ("marketing", "demand"): "marketing_demand",
    ("marketing", "seo"): "marketing_seo",
    ("marketing", "performance"): "marketing_performance",
    ("marketing", "analytics"): "marketing_analytics",
    ("content", "overview"): "content_overview",
    ("content", "calendar"): "content_calendar",
    ("content", "studio"): "content_studio",
    ("content", "editorial"): "content_editorial",
    ("content", "publishing"): "content_publishing",
    ("content", "seo"): "content_seo",
    ("content", "pipeline"): "content_pipeline",
    ("content", "analytics"): "content_analytics",
    ("operations", "overview"): "operations_overview",
    ("operations", "approvals"): "operations_approvals",
    ("operations", "activity"): "operations_activity",
    ("operations", "automations"): "operations_automations",
    ("operations", "jobs"): "operations_jobs",
    ("agents", "overview"): "agents_hub",
    ("agents", "runs"): "agents_runs",
    ("agents", "capabilities"): "agents_capabilities",
    ("agents", "evaluations"): "agents_evaluations",
    ("system", "overview"): "system_overview",
    ("system", "integrations"): "system_integrations",
    ("system", "governance"): "system_governance",
    ("system", "identity"): "system_identity",
    ("system", "tenant"): "system_tenant",
    ("system", "health"): "system_health",
    ("system", "settings"): "system_settings",
    ("design-system", "overview"): "design_system",
}


# active_page values that should expand the Content section of the shell.
CONTENT_ACTIVE_PAGES: frozenset[str] = frozenset(
    {
        "dashboard",
        "cockpit",
        "analytics",
        "weeks",
        "content_studio",
        "editorial",
        "publishing",
        "seo",
        "pipeline",
        "content_ops",
        "os_content",
        "os_content_calendar",
        "os_content_studio",
        "os_content_editorial",
        "os_content_publishing",
        "os_content_seo",
        "os_content_pipeline",
        "os_content_analytics",
    }
)
