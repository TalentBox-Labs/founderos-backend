"""Redact sensitive query/log fields without changing request handling."""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qsl, urlencode

# Values that must never appear in application or access logs.
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "code",
        "state",
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "password",
        "client_secret",
        "secret",
        "authorization",
        "cookie",
        "jwt",
    }
)

# uvicorn.access logs the raw request line including query string.
_ACCESS_QUERY_RE = re.compile(r"(\"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+)([^?\s\"]+)\?([^\s\"]+)")


def redact_query_string(query_string: str) -> str:
    """Return a query string with sensitive parameter values replaced."""
    if not query_string:
        return ""
    redacted: list[tuple[str, str]] = []
    for key, value in parse_qsl(query_string, keep_blank_values=True):
        if key.lower() in _SENSITIVE_QUERY_KEYS:
            redacted.append((key, "[REDACTED]"))
        else:
            redacted.append((key, value))
    return urlencode(redacted)


def redact_access_log_message(message: str) -> str:
    """Redact sensitive query values inside a uvicorn-style access log line."""
    if not message or "?" not in message:
        return message

    def _sub(match: re.Match[str]) -> str:
        prefix, path, query = match.group(1), match.group(2), match.group(3)
        return f"{prefix}{path}?{redact_query_string(query)}"

    return _ACCESS_QUERY_RE.sub(_sub, message)


class RedactAccessLogFilter(logging.Filter):
    """Filter that redacts OAuth/credential query params from access log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = redact_access_log_message(msg)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True
