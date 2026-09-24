"""DATABASE_URL and SECRET_KEY validation for Founder OS runtime.

SQLite URLs remain allowed for local automated tests only.
PostgreSQL URLs must request unambiguous TLS (sslmode=require or stronger).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

# Minimum SECRET_KEY length after strip. Prefer secrets.token_urlsafe(32)+.
SECRET_KEY_MIN_LENGTH = 32

FORBIDDEN_SECRET_KEYS = frozenset(
    {
        "",
        "change-me-in-production",
        "replace-with-strong-random-value",
        "secret",
        "password",
        "password123",
        "changeme",
        "change-me",
        "admin",
        "test",
        "test-secret",
        "test-secret-key",
        "dev",
        "development",
        "staging",
        "production",
        "founder",
        "founderos",
        "workcrew",
    }
)

FORBIDDEN_BOOTSTRAP_PASSWORDS = frozenset(
    {
        "",
        "password",
        "password123",
        "changeme",
        "change-me",
        "change-me-in-production",
        "replace-with-strong-random-value",
        "secret",
        "admin",
        "founder",
        "test",
        "test-password",
    }
)

_SECURE_SSLMODES = frozenset({"require", "verify-ca", "verify-full"})
_INSECURE_SSLMODES = frozenset({"disable", "allow", "prefer", ""})
_SECURE_SSL_FLAGS = frozenset({"1", "true", "require"})
_INSECURE_SSL_FLAGS = frozenset({"0", "false", "disable"})

# Patterns that often appear in driver/SQLAlchemy exception text.
_CREDENTIAL_LEAK_RE = re.compile(
    r"(?i)(postgresql(\+[a-z0-9_]+)?:\/\/[^\s'\"]+|password\s*[:=]\s*\S+|pwd\s*[:=]\s*\S+)"
)


def redact_database_url(url: str) -> str:
    """Return a log-safe form of a database URL (credentials stripped)."""
    if not url or not str(url).strip():
        return "<empty>"
    raw = str(url).strip()
    try:
        parsed = urlparse(raw)
        if not parsed.scheme:
            return "<redacted>"
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        path = parsed.path or ""
        user = parsed.username or ""
        user_part = f"{user}:***@" if user else ("***@" if parsed.password else "")
        return f"{parsed.scheme}://{user_part}{host}{port}{path}"
    except Exception:
        return "<redacted>"


def sanitize_exception_for_log(exc: BaseException | None) -> str:
    """Return a credential-safe one-line description of an exception type."""
    if exc is None:
        return "Exception"
    name = type(exc).__name__
    # Never include str(exc)/repr(exc) — drivers often embed DSN/password.
    return name


def text_contains_credential_material(text: str, *, sentinel_password: str | None = None) -> bool:
    """Heuristic used by tests to assert logs do not contain credential material."""
    if not text:
        return False
    if sentinel_password and sentinel_password in text:
        return True
    if "SENTINEL_DB_PASSWORD" in text:
        return True
    if _CREDENTIAL_LEAK_RE.search(text):
        return True
    return False


def validate_secret_key(value: str | None) -> str:
    """Fail closed for missing, whitespace, placeholder, or trivial SECRET_KEY values."""
    if value is None:
        raise ValueError(
            "FATAL: SECRET_KEY must be set to a strong random value. "
            "Generate with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    if value != value.strip():
        raise ValueError(
            "FATAL: SECRET_KEY must not include leading or trailing whitespace."
        )
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(
            "FATAL: SECRET_KEY must be set to a strong random value (whitespace-only rejected)."
        )
    if cleaned.lower() in FORBIDDEN_SECRET_KEYS:
        raise ValueError(
            "FATAL: SECRET_KEY matches a forbidden placeholder/default value."
        )
    if len(cleaned) < SECRET_KEY_MIN_LENGTH:
        raise ValueError(
            f"FATAL: SECRET_KEY must be at least {SECRET_KEY_MIN_LENGTH} characters after strip."
        )
    if len(set(cleaned)) == 1:
        raise ValueError("FATAL: SECRET_KEY must not be a repeated single character.")
    return cleaned


def _query_map(parsed) -> dict[str, list[str]]:
    return parse_qs(parsed.query, keep_blank_values=True)


def _normalize_tls_values(values: list[str]) -> list[str]:
    return [str(v).strip().lower() for v in values]


def assert_postgres_tls_unambiguous(url: str) -> None:
    """Reject missing, insecure, duplicate, or contradictory TLS query parameters.

    Duplicate security-sensitive TLS parameters (sslmode / ssl) are always rejected,
    even when values are identical, so security never depends on parse order.
    """
    parsed = urlparse(url.strip())
    q = {k.lower(): _normalize_tls_values(vals) for k, vals in _query_map(parsed).items()}

    sslmodes = q.get("sslmode") or []
    ssls = q.get("ssl") or []

    if len(sslmodes) > 1:
        raise ValueError(
            "FATAL: DATABASE_URL must not declare sslmode more than once "
            "(ambiguous TLS configuration)."
        )
    if len(ssls) > 1:
        raise ValueError(
            "FATAL: DATABASE_URL must not declare ssl more than once "
            "(ambiguous TLS configuration)."
        )

    sslmode = sslmodes[0] if sslmodes else None
    ssl = ssls[0] if ssls else None

    if sslmode is not None and sslmode in _INSECURE_SSLMODES:
        raise ValueError(
            "FATAL: DATABASE_URL sslmode must be require, verify-ca, or verify-full."
        )
    if ssl is not None and ssl in _INSECURE_SSL_FLAGS:
        raise ValueError("FATAL: DATABASE_URL ssl=false/0/disable is not permitted.")

    secure_mode = sslmode in _SECURE_SSLMODES if sslmode is not None else False
    secure_ssl = ssl in _SECURE_SSL_FLAGS if ssl is not None else False

    if sslmode is not None and ssl is not None:
        if secure_mode != secure_ssl:
            raise ValueError(
                "FATAL: DATABASE_URL ssl and sslmode contradict each other."
            )
        if not secure_mode:
            raise ValueError(
                "FATAL: DATABASE_URL TLS parameters are not secure."
            )
        return

    if secure_mode or secure_ssl:
        return

    raise ValueError(
        "FATAL: PostgreSQL DATABASE_URL must enable TLS "
        "(include sslmode=require or an equivalent ssl=true). "
        "Example shape: postgresql://USER:PASSWORD@HOST/DB?sslmode=require"
    )


def postgres_tls_configured(url: str) -> bool:
    """True when URL has unambiguous secure TLS; False on missing/insecure TLS.

    Ambiguous duplicates raise ValueError (fail closed) rather than returning False.
    """
    try:
        assert_postgres_tls_unambiguous(url)
        return True
    except ValueError as exc:
        msg = str(exc)
        if "ambiguous" in msg or "contradict" in msg:
            raise
        return False


def validate_database_url(url: str | None) -> str:
    """Validate DATABASE_URL; raise ValueError with a safe (non-secret) message."""
    if url is None or not str(url).strip():
        raise ValueError(
            "FATAL: DATABASE_URL must be set. "
            "For Neon Free staging use a direct PostgreSQL URL with sslmode=require. "
            "Do not use the application default or an empty value."
        )
    cleaned = str(url).strip()
    parsed = urlparse(cleaned)
    scheme = (parsed.scheme or "").lower()

    if scheme in {"sqlite", "sqlite+pysqlite"}:
        return cleaned

    if scheme in {"postgresql", "postgres", "postgresql+psycopg2", "postgresql+psycopg"}:
        assert_postgres_tls_unambiguous(cleaned)
        if not parsed.hostname:
            raise ValueError("FATAL: DATABASE_URL is missing a hostname.")
        return cleaned

    raise ValueError(
        "FATAL: DATABASE_URL scheme is not supported. "
        "Use postgresql://…?sslmode=require (staging) or sqlite://… (tests only)."
    )


def max_app_pool_connections(pool_size: int, max_overflow: int) -> int:
    return int(pool_size) + int(max_overflow)


def assert_pool_within_limit(pool_size: int, max_overflow: int, limit: int = 30) -> None:
    total = max_app_pool_connections(pool_size, max_overflow)
    if total > limit:
        raise ValueError(
            f"FATAL: SQLAlchemy pool_size+max_overflow={total} exceeds limit {limit}."
        )


def bootstrap_password_acceptable(password: str | None) -> bool:
    if password is None:
        return False
    cleaned = password.strip()
    if len(cleaned) < 12:
        return False
    if cleaned.lower() in FORBIDDEN_BOOTSTRAP_PASSWORDS:
        return False
    if cleaned == unquote(cleaned) and cleaned.lower() in FORBIDDEN_BOOTSTRAP_PASSWORDS:
        return False
    return True


def validate_recovery_secret(value: str | None) -> str:
    """Fail closed for missing/weak FOUNDER_OS_RECOVERY_SECRET.

    Independent of SECRET_KEY / DATABASE_URL / bootstrap password / Google secrets.
    Same strength floor as SECRET_KEY (length + placeholder rejection).
    """
    if value is None:
        raise ValueError(
            "FATAL: FOUNDER_OS_RECOVERY_SECRET must be set to a strong random value. "
            "Generate with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    if value != value.strip():
        raise ValueError(
            "FATAL: FOUNDER_OS_RECOVERY_SECRET must not include leading or trailing whitespace."
        )
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(
            "FATAL: FOUNDER_OS_RECOVERY_SECRET must be set to a strong random value "
            "(whitespace-only rejected)."
        )
    if cleaned.lower() in FORBIDDEN_SECRET_KEYS:
        raise ValueError(
            "FATAL: FOUNDER_OS_RECOVERY_SECRET matches a forbidden placeholder/default value."
        )
    if len(cleaned) < SECRET_KEY_MIN_LENGTH:
        raise ValueError(
            "FATAL: FOUNDER_OS_RECOVERY_SECRET must be at least "
            f"{SECRET_KEY_MIN_LENGTH} characters after strip."
        )
    if len(set(cleaned)) == 1:
        raise ValueError(
            "FATAL: FOUNDER_OS_RECOVERY_SECRET must not be a repeated single character."
        )
    return cleaned
