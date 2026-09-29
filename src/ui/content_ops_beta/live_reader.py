"""Live Content Ops reader.

Calls the same projection as GET /api/v1/content-ops/weeks/current.
A failed read stays an error. It does not load the mock adapter.
"""

from __future__ import annotations

import logging
from typing import Any

from src.tools.content_ops_current_week import CONTRACT, read_current_week
from src.ui.content_ops_beta.contract import TARGET_READ_CONTRACT

logger = logging.getLogger(__name__)

_STATUS_ERRORS = {
    401: ("UNAUTHORIZED", "Sign in to view Content Ops."),
    403: (
        "FORBIDDEN",
        "This account cannot open Content Ops for the beta organization.",
    ),
    409: (
        "ACTIVE_INTENT",
        "A publication action is already in progress for the current week.",
    ),
    422: (
        "POLICY_FAILURE",
        "The request did not meet Content Ops policy. Nothing was published.",
    ),
}


class LiveContentOpsReadAdapter:
    """Server projection. Scenario text is not a week or tenant selector."""

    def read_current_week(self, scenario: str | None = None) -> dict[str, Any]:
        del scenario
        try:
            payload = read_current_week()
        except Exception:
            logger.exception("Content Ops current-week read failed")
            return server_error_envelope()
        if not isinstance(payload, dict):
            return server_error_envelope()
        body = dict(payload)
        body["source"] = body.get("source") or "content_ops_publication_truth"
        body["live"] = True
        body["contract"] = body.get("contract") or CONTRACT
        body["scenario"] = ""
        body["available_scenarios"] = []
        return body


def envelope_for_status(status: int) -> dict[str, Any]:
    """Fixed copy for HTTP errors. Exception text is not copied onto the page."""
    if status >= 500:
        return server_error_envelope()
    code, message = _STATUS_ERRORS.get(
        status,
        ("READ_FAILED", "The Content Ops read failed. No week was loaded."),
    )
    return _error(status, code, message)


def server_error_envelope() -> dict[str, Any]:
    return _error(
        500,
        "SERVER_ERROR",
        "Content Ops could not load the current week.",
    )


def _error(status: int, code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "source": "content_ops_publication_truth",
        "live": True,
        "contract": TARGET_READ_CONTRACT,
        "scenario": "",
        "http_status": status,
        "state": "error",
        "reason": code.lower(),
        "projection": None,
        "error": {"code": code, "message": message},
        "available_scenarios": [],
    }
