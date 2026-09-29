"""Content Ops authority boundary.

Tenant model: EXPLICIT_SINGLE_ORG_BETA_LOCK

Content artifacts (tracker rows, editorial decisions, publish jobs) have no
organization column. Filtering them by a client organization id would invent
multi-tenant isolation. Until a canonical schema can store that binding,
Content Ops is locked to one server-resolved organization:

- ``FOUNDER_OS_CONTENT_OPS_BETA_ORGANIZATION_ID`` when the process environment
  pins an active organization (never a request field)
- otherwise the sole ACTIVE organization in the database
- more than one active organization and no pin → deny

The caller must be an authenticated HUMAN with exactly one active membership,
and that membership must be the beta organization.

Not authority: service/API keys, OAuth callback parameters, query/header/body
organization ids, and the organization cookie. A free-text approver or
requested_by value never upgrades SERVICE into a human operator.

Machine publication is not a hidden alias of these human routes. There is no
service publication capability on this boundary.
"""

from __future__ import annotations

import logging
import os
import uuid

from fastapi import HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

import revenue_os.services.tenant_resolution as tenant_resolution
from revenue_os.models.organization import Organization, OrganizationStatus
from revenue_os.services.identity_context import PrincipalKind, bind_requested_by
from revenue_os.services.tenant_context import (
    TenantContext,
    assert_valid_membership_role,
)
from runner_api_routers.identity import (
    _bearer_matches_runner_api_key,
    identity_from_request,
)

logger = logging.getLogger(__name__)

TENANT_MODEL = "EXPLICIT_SINGLE_ORG_BETA_LOCK"
CONTENT_OPS_BETA_ORGANIZATION_ENV = "FOUNDER_OS_CONTENT_OPS_BETA_ORGANIZATION_ID"

_MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_CONTENT_OPS_HTML_EXACT = frozenset(
    {
        "/",
        "/weeks",
        "/content-studio",
        "/editorial",
        "/publishing",
        "/pipeline",
        "/marketing",
        "/sales",
        "/seo",
        "/analytics",
        "/mcp",
    }
)
_CONTENT_OPS_HTML_PREFIXES = (
    "/weeks/",
    "/content-studio/",
    "/editorial/",
    "/publishing/",
    "/seo/",
)


def is_content_ops_html_path(path: str) -> bool:
    cleaned = (path or "").split("?", 1)[0].rstrip("/") or "/"
    if cleaned in _CONTENT_OPS_HTML_EXACT:
        return True
    return any(cleaned.startswith(prefix) for prefix in _CONTENT_OPS_HTML_PREFIXES)


def _as_uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _human_session(request: Request):
    ctx = identity_from_request(request)
    if (
        ctx is not None
        and ctx.principal_kind is PrincipalKind.HUMAN
        and ctx.is_human
        and ctx.user_id
    ):
        return ctx
    return None


def _beta_organization_id(db) -> uuid.UUID | None:
    """Server pin, or the sole active organization. Never a request value."""
    pinned = os.environ.get(CONTENT_OPS_BETA_ORGANIZATION_ENV, "").strip()
    if pinned:
        try:
            org_id = uuid.UUID(pinned)
        except ValueError:
            return None
        org = db.get(Organization, org_id)
        if org is None or org.status != OrganizationStatus.ACTIVE:
            return None
        return org_id

    orgs = (
        db.query(Organization.id)
        .filter(Organization.status == OrganizationStatus.ACTIVE)
        .all()
    )
    if len(orgs) != 1:
        return None
    return _as_uuid(orgs[0][0])


def resolve_content_ops_tenant(request: Request) -> TenantContext:
    """Bind Content Ops to the human member of the server beta organization.

    Query, header, body, OAuth state, and the organization cookie are not read.
    """
    human = _human_session(request)
    if human is None:
        if _bearer_matches_runner_api_key(request):
            raise HTTPException(
                status_code=403,
                detail="SERVICE identity cannot exercise human or tenant authority",
            )
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        user_uuid = uuid.UUID(str(human.user_id))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Authentication required") from exc

    db = tenant_resolution.SessionLocal()
    try:
        try:
            memberships = tenant_resolution._active_memberships(db, user_uuid)
            beta_id = _beta_organization_id(db)
            bound = [
                {
                    "membership_id": str(row.id),
                    "organization_id": _as_uuid(row.organization_id),
                    "role": row.role,
                    "status": row.status.value
                    if hasattr(row.status, "value")
                    else str(row.status),
                }
                for row in memberships
            ]
            org = db.get(Organization, beta_id) if beta_id is not None else None
            org_name = org.name if org is not None else ""
            org_slug = org.slug if org is not None else ""
            org_active = org is not None and org.status == OrganizationStatus.ACTIVE
        except SQLAlchemyError as exc:
            logger.warning(
                "Content Ops tenant resolution failed: %s",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail="Tenant resolution unavailable",
            ) from exc
    finally:
        db.close()

    if not bound:
        raise HTTPException(
            status_code=403,
            detail="Organization membership required",
        )
    if beta_id is None or not org_active:
        raise HTTPException(
            status_code=403,
            detail="Content Ops beta organization is not uniquely bound",
        )
    if len(bound) != 1:
        raise HTTPException(
            status_code=403,
            detail="Organization context is ambiguous",
        )
    selected = bound[0]
    if selected["organization_id"] != beta_id:
        raise HTTPException(
            status_code=403,
            detail="Not a member of the Content Ops beta organization",
        )

    return TenantContext(
        identity=human,
        organization_id=str(beta_id),
        organization_name=org_name,
        organization_slug=org_slug,
        membership_id=selected["membership_id"],
        membership_role=assert_valid_membership_role(selected["role"] or "member"),
        membership_status=selected["status"],
    )


def require_content_ops_access(request: Request) -> str:
    """Human beta-tenant gate for Content Ops reads and mutations."""
    tenant = resolve_content_ops_tenant(request)
    if request.method.upper() in _MUTATION_METHODS:
        tenant_resolution.require_tenant_mutation_role(tenant)
    return tenant.organization_id


def enforce_content_ops_html_tenant(request: Request) -> None:
    """Fail closed for Content Ops HTML once a human session exists."""
    if not is_content_ops_html_path(request.url.path):
        return
    resolve_content_ops_tenant(request)


def content_ops_human_actor(request: Request, client_supplied: str) -> str:
    """Authority is the authenticated human, never a client display name.

    A service bearer cannot be relabeled with approver/requested_by text.
    Production requests without a human session never reach this function:
    ``require_human_or_api_key`` rejects them first. The client label is used
    only when no session and no Authorization header are present, which is the
    in-process route harness that replaces that dependency.
    """
    human = _human_session(request)
    if _bearer_matches_runner_api_key(request) and human is None:
        raise HTTPException(
            status_code=403,
            detail="SERVICE identity cannot exercise human authority",
        )
    if human is not None:
        try:
            return bind_requested_by(human)
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail="Human authority required",
            ) from exc
    ctx = identity_from_request(request)
    if ctx is not None and not ctx.is_human:
        raise HTTPException(
            status_code=403,
            detail="SERVICE identity cannot exercise human authority",
        )
    if request.headers.get("authorization"):
        raise HTTPException(
            status_code=403,
            detail="SERVICE identity cannot exercise human authority",
        )
    return (client_supplied or "").strip()
