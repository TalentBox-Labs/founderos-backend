"""Credential-safe Gmail OAuth callback authority diagnostics.

Reports ONLY categorical booleans / bounded enums. Never logs cookie, JWT,
OAuth state, authorization codes, tokens, org UUIDs, user IDs, or secrets.

Does not grant authority and must not change OAuth/tenant fail-closed semantics.
"""

from __future__ import annotations

import logging
import uuid as uuid_lib
from typing import Any, Mapping

from fastapi import Request
from jose import JWTError, jwt
from sqlalchemy.exc import SQLAlchemyError

from revenue_os.config import settings
from revenue_os.database import SessionLocal
from revenue_os.models.organization import (
    MembershipStatus,
    Organization,
    OrganizationMembership,
    OrganizationStatus,
)
from revenue_os.models.user import User
from revenue_os.services.identity_context import PrincipalKind
from revenue_os.services.session_revocation import (
    SessionRevocationStoreUnavailable,
    is_jti_revoked,
)
from revenue_os.services.tenant_context import parse_org_uuid
from revenue_os.services.tenant_resolution import ORGANIZATION_COOKIE
from runner_api_routers.identity import IDENTITY_COOKIE
from src.tools.editorial_approval import is_human_approver

logger = logging.getLogger(__name__)

FAILURE_STAGES = frozenset(
    {
        "NO_SESSION_COOKIE",
        "INVALID_SESSION",
        "REVOKED_SESSION",
        "TOKEN_VERSION_MISMATCH",
        "HUMAN_NOT_RESOLVED",
        "ZERO_MEMBERSHIP",
        "MULTI_ORG_SELECTION_REQUIRED",
        "SELECTED_ORG_INVALID",
        "TENANT_RESOLUTION_FAILED",
        "STATE_MISSING",
        "STATE_INVALID",
        "STATE_CORRELATION_FAILED",
        "AUTHORIZATION_CODE_MISSING",
        "POST_AUTHORITY_FAILURE",
        "SUCCESS",
        "OTHER_SAFE_CLASSIFICATION",
    }
)

_DIAG_KEYS = (
    "request_present",
    "session_cookie_present",
    "org_cookie_present",
    "identity_resolved",
    "is_human",
    "session_valid",
    "revocation_valid",
    "token_version_valid",
    "membership_count",
    "selected_org_present",
    "tenant_resolved",
    "state_present",
    "state_signature_valid",
    "state_correlation_valid",
    "authorization_code_present",
    "failure_stage",
)

_BOOL_KEYS = frozenset(
    {
        "request_present",
        "session_cookie_present",
        "org_cookie_present",
        "identity_resolved",
        "is_human",
        "session_valid",
        "revocation_valid",
        "token_version_valid",
        "selected_org_present",
        "tenant_resolved",
        "state_present",
        "state_signature_valid",
        "state_correlation_valid",
        "authorization_code_present",
    }
)

_GMAIL_OAUTH_STATE_PURPOSE = "gmail_oauth"


def _as_bool(value: Any) -> bool:
    return bool(value)


def _membership_bucket(count: int) -> str:
    if count <= 0:
        return "0"
    if count == 1:
        return "1"
    return "many"


def _empty_diagnostic() -> dict[str, Any]:
    return {
        "request_present": False,
        "session_cookie_present": False,
        "org_cookie_present": False,
        "identity_resolved": False,
        "is_human": False,
        "session_valid": False,
        "revocation_valid": False,
        "token_version_valid": False,
        "membership_count": "0",
        "selected_org_present": False,
        "tenant_resolved": False,
        "state_present": False,
        "state_signature_valid": False,
        "state_correlation_valid": False,
        "authorization_code_present": False,
        "failure_stage": "OTHER_SAFE_CLASSIFICATION",
    }


def sanitize_gmail_callback_diagnostic(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Return only allowlisted categorical fields with coerced safe types."""
    out = _empty_diagnostic()
    for key in _BOOL_KEYS:
        if key in fields:
            out[key] = _as_bool(fields[key])
    mc = fields.get("membership_count", "0")
    if mc in ("0", "1", "many"):
        out["membership_count"] = mc
    else:
        out["membership_count"] = "0"
    stage = str(fields.get("failure_stage") or "OTHER_SAFE_CLASSIFICATION")
    out["failure_stage"] = (
        stage if stage in FAILURE_STAGES else "OTHER_SAFE_CLASSIFICATION"
    )
    return out


def format_gmail_callback_diagnostic_line(fields: Mapping[str, Any]) -> str:
    safe = sanitize_gmail_callback_diagnostic(fields)
    parts = [f"gmail_callback.{key}={safe[key]}" for key in _DIAG_KEYS]
    # booleans as true/false for log grepping
    line_parts = []
    for part in parts:
        key, _, val = part.partition("=")
        if val in ("True", "False"):
            val = val.lower()
        line_parts.append(f"{key}={val}")
    return "gmail_callback.authority_diagnostic " + " ".join(line_parts)


def emit_gmail_callback_diagnostics(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Log and return sanitized categorical diagnostic fields."""
    safe = sanitize_gmail_callback_diagnostic(fields)
    logger.info(format_gmail_callback_diagnostic_line(safe))
    return safe


def _decode_state_org(state: str | None) -> tuple[bool, bool, str | None]:
    """Return (state_present, signature_valid, org_id_or_none). Never logs state."""
    if not state or not str(state).strip():
        return False, False, None
    try:
        payload = jwt.decode(
            str(state).strip(), settings.SECRET_KEY, algorithms=["HS256"]
        )
    except JWTError:
        return True, False, None
    if payload.get("purpose") != _GMAIL_OAUTH_STATE_PURPOSE:
        return True, False, None
    raw_org = payload.get("organization_id")
    if not raw_org:
        return True, False, None
    try:
        return True, True, str(uuid_lib.UUID(str(raw_org)))
    except ValueError:
        return True, False, None


def diagnose_gmail_callback_authority(
    request: Request | None,
    *,
    state: str | None = None,
    code: str | None = None,
    oauth_error: str | None = None,
) -> dict[str, Any]:
    """Classify the earliest authority failure without changing auth semantics."""
    diag = _empty_diagnostic()
    diag["request_present"] = request is not None
    diag["authorization_code_present"] = bool(code and str(code).strip())
    state_present, state_sig_ok, state_org = _decode_state_org(state)
    diag["state_present"] = state_present
    diag["state_signature_valid"] = state_sig_ok

    if oauth_error:
        # Google returned an error before our authority chain completes usefully.
        diag["failure_stage"] = "OTHER_SAFE_CLASSIFICATION"
        return sanitize_gmail_callback_diagnostic(diag)

    if not diag["authorization_code_present"]:
        diag["failure_stage"] = "AUTHORIZATION_CODE_MISSING"
        return sanitize_gmail_callback_diagnostic(diag)

    if request is None:
        diag["failure_stage"] = "OTHER_SAFE_CLASSIFICATION"
        return sanitize_gmail_callback_diagnostic(diag)

    session_raw = request.cookies.get(IDENTITY_COOKIE)
    org_raw = request.cookies.get(ORGANIZATION_COOKIE)
    diag["session_cookie_present"] = bool(session_raw)
    diag["org_cookie_present"] = bool(org_raw)
    diag["selected_org_present"] = bool(org_raw and parse_org_uuid(org_raw) is not None)

    if not session_raw:
        diag["failure_stage"] = "NO_SESSION_COOKIE"
        return sanitize_gmail_callback_diagnostic(diag)

    try:
        payload = jwt.decode(session_raw, settings.SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        diag["failure_stage"] = "INVALID_SESSION"
        return sanitize_gmail_callback_diagnostic(diag)

    diag["session_valid"] = True

    jti = payload.get("jti")
    if isinstance(jti, str) and jti:
        try:
            if is_jti_revoked(jti, session_factory=SessionLocal):
                diag["revocation_valid"] = False
                diag["failure_stage"] = "REVOKED_SESSION"
                return sanitize_gmail_callback_diagnostic(diag)
            diag["revocation_valid"] = True
        except SessionRevocationStoreUnavailable:
            diag["revocation_valid"] = False
            diag["failure_stage"] = "OTHER_SAFE_CLASSIFICATION"
            return sanitize_gmail_callback_diagnostic(diag)
    else:
        # Missing jti is treated as revocation-check N/A but session still usable
        # by identity_from_request (only checks when jti present).
        diag["revocation_valid"] = True

    kind_raw = payload.get("kind") or PrincipalKind.HUMAN.value
    try:
        kind = PrincipalKind(str(kind_raw))
    except ValueError:
        diag["failure_stage"] = "INVALID_SESSION"
        return sanitize_gmail_callback_diagnostic(diag)

    if kind is not PrincipalKind.HUMAN:
        diag["identity_resolved"] = True
        diag["is_human"] = False
        diag["failure_stage"] = "HUMAN_NOT_RESOLVED"
        return sanitize_gmail_callback_diagnostic(diag)

    sub = str(payload.get("sub") or "") or None
    if not sub:
        diag["failure_stage"] = "HUMAN_NOT_RESOLVED"
        return sanitize_gmail_callback_diagnostic(diag)

    try:
        user_uuid = uuid_lib.UUID(sub)
    except ValueError:
        diag["failure_stage"] = "HUMAN_NOT_RESOLVED"
        return sanitize_gmail_callback_diagnostic(diag)

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_uuid).first()
        if user is None or not user.is_active:
            diag["failure_stage"] = "HUMAN_NOT_RESOLVED"
            return sanitize_gmail_callback_diagnostic(diag)

        try:
            claim_tv = int(payload["tv"]) if "tv" in payload else 0
        except (TypeError, ValueError):
            diag["failure_stage"] = "INVALID_SESSION"
            return sanitize_gmail_callback_diagnostic(diag)
        current_tv = int(getattr(user, "token_version", 0) or 0)
        if claim_tv != current_tv:
            diag["token_version_valid"] = False
            diag["failure_stage"] = "TOKEN_VERSION_MISMATCH"
            return sanitize_gmail_callback_diagnostic(diag)
        diag["token_version_valid"] = True

        name = (user.full_name or "").strip()
        human = bool(is_human_approver(name))
        diag["identity_resolved"] = True
        diag["is_human"] = human
        if not human:
            diag["failure_stage"] = "HUMAN_NOT_RESOLVED"
            return sanitize_gmail_callback_diagnostic(diag)

        memberships = (
            db.query(OrganizationMembership)
            .join(Organization)
            .filter(
                OrganizationMembership.user_id == user.id,
                OrganizationMembership.status == MembershipStatus.ACTIVE,
                Organization.status == OrganizationStatus.ACTIVE,
            )
            .all()
        )
        diag["membership_count"] = _membership_bucket(len(memberships))
        if not memberships:
            diag["failure_stage"] = "ZERO_MEMBERSHIP"
            return sanitize_gmail_callback_diagnostic(diag)

        org_uuid = parse_org_uuid(org_raw)
        selected: OrganizationMembership | None = None
        if org_uuid is not None:
            selected = (
                db.query(OrganizationMembership)
                .filter(
                    OrganizationMembership.user_id == user.id,
                    OrganizationMembership.organization_id == org_uuid,
                )
                .first()
            )
            if selected is None or selected.status != MembershipStatus.ACTIVE:
                diag["failure_stage"] = "SELECTED_ORG_INVALID"
                return sanitize_gmail_callback_diagnostic(diag)
        elif len(memberships) == 1:
            selected = memberships[0]
        else:
            diag["failure_stage"] = "MULTI_ORG_SELECTION_REQUIRED"
            return sanitize_gmail_callback_diagnostic(diag)

        org = db.get(Organization, selected.organization_id)
        if org is None or org.status != OrganizationStatus.ACTIVE:
            diag["failure_stage"] = "SELECTED_ORG_INVALID"
            return sanitize_gmail_callback_diagnostic(diag)

        persist_org = str(org.id)
        diag["tenant_resolved"] = True

        if not state_present:
            diag["failure_stage"] = "STATE_MISSING"
            return sanitize_gmail_callback_diagnostic(diag)
        if not state_sig_ok or state_org is None:
            diag["failure_stage"] = "STATE_INVALID"
            return sanitize_gmail_callback_diagnostic(diag)
        if state_org != persist_org:
            diag["state_correlation_valid"] = False
            diag["failure_stage"] = "STATE_CORRELATION_FAILED"
            return sanitize_gmail_callback_diagnostic(diag)

        diag["state_correlation_valid"] = True
        diag["failure_stage"] = "SUCCESS"
        return sanitize_gmail_callback_diagnostic(diag)
    except SQLAlchemyError:
        diag["failure_stage"] = "TENANT_RESOLUTION_FAILED"
        return sanitize_gmail_callback_diagnostic(diag)
    finally:
        db.close()
