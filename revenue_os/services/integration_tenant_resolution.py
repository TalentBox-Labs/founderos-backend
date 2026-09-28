"""S4 — integration tenant resolution (webhook / service binding)."""

from __future__ import annotations

import logging
import os
import uuid as uuid_lib

from revenue_os.services.identity_context import AuthMethod, IdentityContext, PrincipalKind
from revenue_os.services.tenant_context import TenantContext

logger = logging.getLogger(__name__)

INTEGRATION_N8N = "n8n"


def resolve_n8n_organization_id(*, x_n8n_secret: str | None) -> str | None:
    """Resolve Organization from trusted integration secret — never from payload object IDs."""
    secret = (x_n8n_secret or "").strip()
    if secret:
        try:
            from revenue_os.database import SessionLocal
            from revenue_os.models.integrations import OrganizationIntegrationBinding

            db = SessionLocal()
            try:
                row = (
                    db.query(OrganizationIntegrationBinding)
                    .filter(
                        OrganizationIntegrationBinding.integration_name == INTEGRATION_N8N,
                        OrganizationIntegrationBinding.inbound_secret == secret,
                    )
                    .first()
                )
                if row is not None:
                    return str(row.organization_id)
            finally:
                db.close()
        except Exception as exc:
            logger.warning("Integration binding lookup failed: %s", exc)

        env_secret = os.environ.get("N8N_INBOUND_SECRET", "").strip()
        env_org = os.environ.get("N8N_INBOUND_ORGANIZATION_ID", "").strip()
        if env_secret and secret == env_secret and env_org:
            try:
                uuid_lib.UUID(env_org)
                return env_org
            except ValueError:
                logger.warning("N8N_INBOUND_ORGANIZATION_ID is not a valid UUID")
    return None


def resolve_integration_org_id(request=None) -> str | None:  # noqa: ANN001
    """Organization from human session/cookie for integration credential CRUD.

    Prefer an explicit ASGI ``request`` when available (OAuth callback continuity).
    Falls back to the bound request ContextVar for ordinary same-process routes.
    """
    from revenue_os.services.tenant_mutation_guard import crm_tenant_org_id
    from revenue_os.services.tenant_resolution import resolve_tenant_context

    return crm_tenant_org_id(resolve_tenant_context(request))


def build_integration_tenant_context(organization_id: str) -> TenantContext:
    """Service/integration TenantContext for scoped object resolution (not human authority)."""
    identity = IdentityContext(
        principal_kind=PrincipalKind.SERVICE,
        auth_method=AuthMethod.API_KEY,
        is_human=False,
        display_name="integration",
    )
    return TenantContext(
        identity=identity,
        organization_id=str(organization_id),
        organization_name="",
        organization_slug="",
        membership_id="integration-binding",
        membership_role="member",
        membership_status="active",
    )


def upsert_n8n_binding(organization_id: str, inbound_secret: str) -> None:
    """Register or update trusted n8n inbound secret for an organization."""
    from revenue_os.database import SessionLocal
    from revenue_os.models.integrations import OrganizationIntegrationBinding

    db = SessionLocal()
    try:
        row = (
            db.query(OrganizationIntegrationBinding)
            .filter(
                OrganizationIntegrationBinding.organization_id == organization_id,
                OrganizationIntegrationBinding.integration_name == INTEGRATION_N8N,
            )
            .first()
        )
        if row is None:
            row = OrganizationIntegrationBinding(
                organization_id=organization_id,
                integration_name=INTEGRATION_N8N,
                inbound_secret=inbound_secret,
            )
            db.add(row)
        else:
            row.inbound_secret = inbound_secret
        db.commit()
    finally:
        db.close()
