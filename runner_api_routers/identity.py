"""SaaS S1 — human identity on primary runner_api.

Authentication: bcrypt-verified User + httpOnly JWT cookie (python-jose).
RUNNER_API_KEY remains SERVICE auth and never confers HUMAN authority.
"""

from __future__ import annotations

import hmac
import logging
import os
import time
import uuid
from collections import defaultdict
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jose import JWTError, jwt
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

from revenue_os.auth import create_access_token, hash_password, verify_password
from revenue_os.config import settings
from revenue_os.database import SessionLocal
from revenue_os.db_url import bootstrap_password_acceptable, sanitize_exception_for_log
from revenue_os.models.user import User
from revenue_os.services.identity_context import (
    AuthMethod,
    IdentityContext,
    PrincipalKind,
    anonymous_identity,
    bind_requested_by,
    classify_actor_label,
    normalize_role,
    service_identity,
)
from revenue_os.services.session_revocation import (
    SessionRevocationStoreUnavailable,
    is_jti_revoked,
    revoke_jti,
    stage_jti_revocation,
)
from src.tools.editorial_approval import is_human_approver

logger = logging.getLogger(__name__)

router = APIRouter(tags=["identity"])

IDENTITY_COOKIE = "founder_os_identity"
IDENTITY_COOKIE_PATH = "/"
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

ENV_OPERATOR = "FOUNDER_OS_OPERATOR_NAME"
ENV_REQUIRE_LOGIN = "FOUNDER_OS_REQUIRE_LOGIN"
ENV_BOOTSTRAP_EMAIL = "FOUNDER_OS_BOOTSTRAP_EMAIL"
ENV_BOOTSTRAP_PASSWORD = "FOUNDER_OS_BOOTSTRAP_PASSWORD"
ENV_BOOTSTRAP_NAME = "FOUNDER_OS_BOOTSTRAP_NAME"

_request_ctx: ContextVar[Request | None] = ContextVar(
    "founder_os_identity_request", default=None
)
# Retained for test fixture compatibility only. Auth uses durable DB revocation.
_revoked_jtis: set[str] = set()
_login_failures: dict[str, list[float]] = defaultdict(list)

_TRUTH_VALUES = frozenset({"1", "true", "yes", "on"})


class LoginJsonBody(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=1, max_length=1024)
    next: str | None = Field(default=None, max_length=512)


class PasswordChangeBody(BaseModel):
    """HUMAN self password change — target is the authenticated session only."""

    current_password: str = Field(..., min_length=1, max_length=1024)
    new_password: str = Field(..., min_length=1, max_length=1024)


def current_request() -> Request | None:
    return _request_ctx.get()


async def bind_request_context(request: Request, call_next: Any) -> Response:
    token = _request_ctx.set(request)
    try:
        return await call_next(request)
    finally:
        _request_ctx.reset(token)


def founder_html_login_required() -> bool:
    raw = os.environ.get(ENV_REQUIRE_LOGIN)
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() in _TRUTH_VALUES
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return True


def safe_next_path(value: str | None) -> str:
    if not value:
        return "/cockpit"
    path = value.strip()
    if not path.startswith("/") or path.startswith("//") or "://" in path:
        return "/cockpit"
    return path


def _cookie_secure() -> bool:
    return os.environ.get("FOUNDER_OS_COOKIE_SECURE", "").strip().lower() in _TRUTH_VALUES


def _max_login_failures() -> int:
    try:
        return max(1, int(os.environ.get("FOUNDER_OS_LOGIN_MAX_FAILURES", "8")))
    except ValueError:
        return 8


def _failure_window_sec() -> float:
    try:
        return max(30.0, float(os.environ.get("FOUNDER_OS_LOGIN_FAILURE_WINDOW_SEC", "900")))
    except ValueError:
        return 900.0


def _prune_failures(email: str, now: float) -> list[float]:
    window = _failure_window_sec()
    kept = [ts for ts in _login_failures[email] if now - ts < window]
    _login_failures[email] = kept
    return kept


def _record_failure(email: str) -> None:
    now = time.monotonic()
    _prune_failures(email, now)
    _login_failures[email].append(now)


def _login_blocked(email: str) -> bool:
    return len(_prune_failures(email, time.monotonic())) >= _max_login_failures()


def _clear_failures(email: str) -> None:
    _login_failures.pop(email, None)


def _set_identity_cookie(response: Response, token: str) -> None:
    max_age = max(60, int(settings.ACCESS_TOKEN_EXPIRE_MINUTES) * 60)
    response.set_cookie(
        key=IDENTITY_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(),
        max_age=max_age,
        path=IDENTITY_COOKIE_PATH,
    )


def _clear_identity_cookie(response: Response) -> None:
    response.delete_cookie(
        key=IDENTITY_COOKIE,
        path=IDENTITY_COOKIE_PATH,
    )


def _maybe_set_organization_cookie(response: Response, user: User) -> None:
    from revenue_os.models.organization import (
        MembershipStatus,
        Organization,
        OrganizationMembership,
        OrganizationStatus,
    )
    from revenue_os.services.tenant_resolution import set_organization_cookie

    db = SessionLocal()
    try:
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
        if len(memberships) == 1:
            set_organization_cookie(response, str(memberships[0].organization_id))
    except Exception:
        logger.debug("Organization cookie not set during login", exc_info=True)
    finally:
        db.close()


def _clear_tenant_cookie(response: Response) -> None:
    from revenue_os.services.tenant_resolution import clear_organization_cookie

    clear_organization_cookie(response)


def _issue_human_token(user: User) -> str:
    jti = str(uuid.uuid4())
    return create_access_token(
        {
            "sub": str(user.id),
            "email": user.email,
            "name": user.full_name,
            "role": normalize_role(user.role),
            "kind": PrincipalKind.HUMAN.value,
            "amr": "password",
            "jti": jti,
            "iss": "founder_os_runner_api",
        }
    )


def _load_user(user_id: str | None) -> User | None:
    if not user_id:
        return None
    try:
        uid = uuid.UUID(str(user_id))
    except ValueError:
        return None
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid).first()
        if user is not None:
            db.expunge(user)
        return user
    except Exception:
        logger.warning("Identity user lookup failed; session not trusted")
        return None
    finally:
        db.close()


def identity_from_request(request: Request | None) -> IdentityContext | None:
    """Return session IdentityContext, or None if no valid identity cookie."""
    if request is None:
        return None
    raw = request.cookies.get(IDENTITY_COOKIE)
    if not raw:
        return None
    try:
        payload = jwt.decode(raw, settings.SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        return None
    jti = payload.get("jti")
    if isinstance(jti, str) and jti:
        try:
            if is_jti_revoked(jti, session_factory=SessionLocal):
                return None
        except SessionRevocationStoreUnavailable:
            # Fail closed: cannot confirm the token is not revoked.
            logger.warning("Identity revocation check unavailable; session not trusted")
            return None
    kind_raw = payload.get("kind") or PrincipalKind.HUMAN.value
    try:
        kind = PrincipalKind(str(kind_raw))
    except ValueError:
        return None
    if kind is not PrincipalKind.HUMAN:
        return IdentityContext(
            principal_kind=kind,
            auth_method=AuthMethod.SESSION,
            is_human=False,
            user_id=str(payload.get("sub") or "") or None,
            email=payload.get("email") if isinstance(payload.get("email"), str) else None,
            display_name=payload.get("name") if isinstance(payload.get("name"), str) else None,
            role=normalize_role(payload.get("role") if isinstance(payload.get("role"), str) else None),
        )
    user = _load_user(str(payload.get("sub") or "") or None)
    if user is None or not user.is_active:
        return None
    name = (user.full_name or "").strip()
    human = bool(is_human_approver(name))
    return IdentityContext(
        principal_kind=PrincipalKind.HUMAN if human else classify_actor_label(name),
        auth_method=AuthMethod.SESSION,
        is_human=human,
        user_id=str(user.id),
        email=user.email,
        display_name=name,
        role=normalize_role(user.role),
    )


def _bearer_matches_runner_api_key(request: Request) -> bool:
    expected = os.environ.get("RUNNER_API_KEY", "").strip()
    if not expected:
        return False
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return False
    provided = header[7:].strip()
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


def identity_context_for_request(request: Request | None) -> IdentityContext:
    if request is None:
        return anonymous_identity()
    session_ctx = identity_from_request(request)
    if session_ctx is not None:
        return session_ctx
    if _bearer_matches_runner_api_key(request):
        return service_identity()
    return anonymous_identity()


def _legacy_env_operator() -> str:
    name = (os.environ.get(ENV_OPERATOR) or "").strip()
    if not is_human_approver(name):
        raise HTTPException(
            status_code=503,
            detail=(
                f"Cockpit human actions require {ENV_OPERATOR} to be set to a valid "
                "human operator name. Client-supplied identity is not accepted."
            ),
        )
    return name


def resolve_trusted_human(request: Request | None = None) -> str:
    """Authoritative human actor for Founder UI proxies.

    Session IdentityContext wins. Legacy FOUNDER_OS_OPERATOR_NAME is fallback only.
    RUNNER_API_KEY never satisfies this function.
    """
    req = request if request is not None else current_request()
    ctx = identity_from_request(req) if req is not None else None
    if ctx is not None and ctx.is_human:
        try:
            return bind_requested_by(ctx)
        except PermissionError as exc:
            raise HTTPException(
                status_code=503,
                detail="Authenticated identity is not a valid human operator.",
            ) from exc
    if ctx is not None and not ctx.is_human:
        kind = ctx.principal_kind.value
        raise HTTPException(
            status_code=503,
            detail=(
                f"{kind} identity cannot perform HUMAN_ONLY actions. "
                "Client-supplied identity is not accepted."
            ),
        )
    return _legacy_env_operator()


def founder_login_redirect(request: Request) -> RedirectResponse | None:
    if not founder_html_login_required():
        return None
    ctx = identity_from_request(request)
    if ctx is not None and ctx.is_human:
        return None
    nxt = quote(request.url.path, safe="/")
    return RedirectResponse(url=f"/login?next={nxt}", status_code=303)


def _authenticate(email: str, password: str) -> User:
    cleaned = email.strip().lower()
    if _login_blocked(cleaned):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try later.")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == cleaned).first()
        if (
            user is None
            or not user.is_active
            or not verify_password(password, user.hashed_password)
        ):
            _record_failure(cleaned)
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if not is_human_approver(user.full_name or ""):
            _record_failure(cleaned)
            raise HTTPException(status_code=401, detail="Invalid credentials")
        _clear_failures(cleaned)
        db.expunge(user)
        return user
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "Login lookup failed: type=%s",
            sanitize_exception_for_log(exc),
        )
        raise HTTPException(status_code=503, detail="Identity store unavailable") from None
    finally:
        db.close()


def _token_expires_at(payload: dict[str, Any]) -> datetime:
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        return datetime.fromtimestamp(float(exp), tz=timezone.utc)
    if isinstance(exp, datetime):
        if exp.tzinfo is None:
            return exp.replace(tzinfo=timezone.utc)
        return exp.astimezone(timezone.utc)
    return datetime.now(timezone.utc) + timedelta(
        minutes=max(1, int(settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    )


def _revoke_request_token(request: Request) -> None:
    """Persist JTI revocation. Raises HTTP 503 if durable store is unavailable."""
    raw = request.cookies.get(IDENTITY_COOKIE)
    if not raw:
        return
    try:
        payload = jwt.decode(raw, settings.SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        return
    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        return
    try:
        revoke_jti(
            jti,
            _token_expires_at(payload),
            session_factory=SessionLocal,
        )
    except SessionRevocationStoreUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Session revocation store unavailable",
        ) from exc


def bootstrap_owner_if_needed() -> None:
    """Create the initial OWNER from env if that email is absent. Never logs the password."""
    from revenue_os.db_url import bootstrap_password_acceptable

    email = os.environ.get(ENV_BOOTSTRAP_EMAIL, "").strip().lower()
    password = os.environ.get(ENV_BOOTSTRAP_PASSWORD, "")
    # Partial bootstrap env is a fail-closed misconfiguration.
    if bool(email) ^ bool(password.strip()):
        logger.error(
            "Identity bootstrap refused: both FOUNDER_OS_BOOTSTRAP_EMAIL and "
            "FOUNDER_OS_BOOTSTRAP_PASSWORD must be set together (or both omitted)"
        )
        return
    if not email or not password:
        return
    if not bootstrap_password_acceptable(password):
        logger.error(
            "Identity bootstrap refused: FOUNDER_OS_BOOTSTRAP_PASSWORD is missing, "
            "too short, or matches a forbidden default/placeholder value"
        )
        return
    name = (
        os.environ.get(ENV_BOOTSTRAP_NAME)
        or os.environ.get(ENV_OPERATOR)
        or ""
    ).strip()
    if not is_human_approver(name):
        logger.warning("Identity bootstrap skipped: bootstrap name is not a valid human")
        return
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == email).first()
        if existing is not None:
            return
        user = User(
            email=email,
            hashed_password=hash_password(password),
            full_name=name,
            role="owner",
            is_active=1,
        )
        db.add(user)
        db.commit()
        logger.info("Bootstrapped OWNER identity for %s", email)
    except Exception as exc:
        logger.error(
            "Identity bootstrap failed: type=%s",
            sanitize_exception_for_log(exc),
        )
        db.rollback()
    finally:
        db.close()

def _wants_html(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        return False
    if "text/html" in accept:
        return True
    return "application/json" not in accept


async def _read_login_payload(request: Request) -> tuple[str, str, str]:
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        body = LoginJsonBody.model_validate(await request.json())
        return body.email, body.password, safe_next_path(body.next)
    form = await request.form()
    email_val = form.get("email")
    password_val = form.get("password")
    next_val = form.get("next")
    if isinstance(email_val, UploadFile) or isinstance(password_val, UploadFile):
        raise HTTPException(status_code=400, detail="Invalid login form")
    email = str(email_val or "")
    password = str(password_val or "")
    nxt = str(next_val) if next_val is not None and not isinstance(next_val, UploadFile) else ""
    return email, password, safe_next_path(nxt or request.query_params.get("next"))


@router.get("/login", response_class=HTMLResponse, response_model=None)
def page_login(request: Request) -> HTMLResponse | RedirectResponse:
    ctx = identity_from_request(request)
    if ctx is not None and ctx.is_human:
        return RedirectResponse(url=safe_next_path(request.query_params.get("next")), status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "request": request,
            "active_page": "login",
            "active_week": "—",
            "identity": None,
            "error": None,
            "next_path": safe_next_path(request.query_params.get("next")),
        },
    )


@router.post("/login", response_model=None)
async def submit_login(request: Request) -> Response:
    email, password, nxt = await _read_login_payload(request)
    try:
        user = _authenticate(email, password)
    except HTTPException as exc:
        if _wants_html(request) and exc.status_code in {401, 429}:
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                status_code=exc.status_code,
                context={
                    "request": request,
                    "active_page": "login",
                    "active_week": "—",
                    "identity": None,
                    "error": exc.detail,
                    "next_path": nxt,
                },
            )
        raise
    token = _issue_human_token(user)
    if _wants_html(request):
        response: Response = RedirectResponse(url=nxt, status_code=303)
    else:
        response = JSONResponse(
            {
                "ok": True,
                "identity": IdentityContext(
                    principal_kind=PrincipalKind.HUMAN,
                    auth_method=AuthMethod.SESSION,
                    is_human=True,
                    user_id=str(user.id),
                    email=user.email,
                    display_name=user.full_name,
                    role=normalize_role(user.role),
                ).as_public_dict(),
            }
        )
    _set_identity_cookie(response, token)
    _maybe_set_organization_cookie(response, user)
    return response


@router.post("/logout", response_model=None)
def submit_logout(request: Request) -> Response:
    # Durable revoke first; cookie clear only after successful revoke (or no token).
    _revoke_request_token(request)
    if _wants_html(request):
        response: Response = RedirectResponse(url="/login", status_code=303)
    else:
        response = JSONResponse({"ok": True, "logged_out": True})
    _clear_identity_cookie(response)
    _clear_tenant_cookie(response)
    return response


@router.get("/api/v1/identity/me")
def identity_me(request: Request) -> dict[str, Any]:
    ctx = identity_context_for_request(request)
    return {"ok": True, "identity": ctx.as_public_dict()}


@router.post("/api/v1/identity/login", response_model=None)
async def api_login(request: Request) -> Response:
    body = LoginJsonBody.model_validate(await request.json())
    user = _authenticate(body.email, body.password)
    token = _issue_human_token(user)
    response = JSONResponse(
        {
            "ok": True,
            "identity": IdentityContext(
                principal_kind=PrincipalKind.HUMAN,
                auth_method=AuthMethod.SESSION,
                is_human=True,
                user_id=str(user.id),
                email=user.email,
                display_name=user.full_name,
                role=normalize_role(user.role),
            ).as_public_dict(),
        }
    )
    _set_identity_cookie(response, token)
    _maybe_set_organization_cookie(response, user)
    return response


@router.post("/api/v1/identity/logout")
def api_logout(request: Request) -> JSONResponse:
    _revoke_request_token(request)
    response = JSONResponse({"ok": True, "logged_out": True})
    _clear_identity_cookie(response)
    _clear_tenant_cookie(response)
    return response


def _require_human_session(request: Request) -> IdentityContext:
    """HUMAN cookie session only. SERVICE/API-key and anonymous fail closed."""
    ctx = identity_from_request(request)
    if (
        ctx is None
        or ctx.principal_kind is not PrincipalKind.HUMAN
        or not ctx.is_human
        or not ctx.user_id
    ):
        raise HTTPException(status_code=401, detail="Authentication required")
    return ctx


def _password_change_rate_key(user_id: str) -> str:
    return f"pwchange:{user_id}"


def _new_password_acceptable(password: str) -> bool:
    # Reuse audited bootstrap strength bar (min length + forbidden placeholders).
    return bootstrap_password_acceptable(password)


@router.post("/api/v1/identity/password")
def api_change_password(request: Request, body: PasswordChangeBody) -> JSONResponse:
    """Authenticated HUMAN changes their own password.

    Authority: session cookie principal only. Body/query/header identity
    assertions are ignored. SERVICE/API-key cannot invoke this endpoint.
    Current password must verify. On success: hash update + current-session
    JTI revocation commit atomically; identity cookie cleared.
    """
    ctx = _require_human_session(request)
    rate_key = _password_change_rate_key(ctx.user_id or "")
    if _login_blocked(rate_key):
        raise HTTPException(status_code=429, detail="Too many attempts. Try later.")

    if not _new_password_acceptable(body.new_password):
        raise HTTPException(status_code=400, detail="Invalid new password")

    if body.current_password == body.new_password:
        raise HTTPException(status_code=400, detail="Invalid new password")

    raw_cookie = request.cookies.get(IDENTITY_COOKIE) or ""
    payload: dict[str, Any] = {}
    jti: str | None = None
    if raw_cookie:
        try:
            payload = jwt.decode(raw_cookie, settings.SECRET_KEY, algorithms=["HS256"])
            maybe_jti = payload.get("jti")
            if isinstance(maybe_jti, str) and maybe_jti.strip():
                jti = maybe_jti.strip()
        except JWTError:
            raise HTTPException(status_code=401, detail="Authentication required") from None

    db = SessionLocal()
    try:
        try:
            uid = uuid.UUID(str(ctx.user_id))
        except ValueError as exc:
            raise HTTPException(status_code=401, detail="Authentication required") from exc

        user = db.query(User).filter(User.id == uid).first()
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="Authentication required")

        if not verify_password(body.current_password, user.hashed_password):
            _record_failure(rate_key)
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if not is_human_approver(user.full_name or ""):
            raise HTTPException(status_code=401, detail="Authentication required")

        user.hashed_password = hash_password(body.new_password)

        if jti is not None:
            try:
                stage_jti_revocation(db, jti, _token_expires_at(payload))
            except SessionRevocationStoreUnavailable as exc:
                db.rollback()
                raise HTTPException(
                    status_code=503,
                    detail="Session revocation store unavailable",
                ) from exc

        db.commit()
        _clear_failures(rate_key)
        logger.info(
            "HUMAN password changed user_id=%s jti_revoked=%s",
            str(user.id),
            "yes" if jti else "no",
        )
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        logger.warning(
            "Password change failed: type=%s",
            sanitize_exception_for_log(exc),
        )
        raise HTTPException(
            status_code=503, detail="Identity store unavailable"
        ) from None
    finally:
        db.close()

    response = JSONResponse({"ok": True})
    _clear_identity_cookie(response)
    _clear_tenant_cookie(response)
    return response
