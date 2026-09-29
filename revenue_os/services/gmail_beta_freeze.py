"""Founder OS beta Gmail deferment control.

Gmail implementation and the PR #36 callback remediation remain in-tree.
Beta critical path defaults to Gmail frozen (operationally disabled) until
explicitly re-enabled via FOUNDER_OS_GMAIL_BETA_ENABLED=1.

This module does not alter HUMAN session, tenant membership, or SERVICE
authority semantics. OAuth state remains correlation-only.
"""

from __future__ import annotations

import os

# Explicit enablement only. Default "0" = frozen for beta.
_ENV_KEY = "FOUNDER_OS_GMAIL_BETA_ENABLED"

_GMAIL_BETA_FROZEN_DETAIL = (
    "Gmail is deferred from the Founder OS beta critical path. "
    "Connect, authorize, sync, and credential mutation are disabled."
)


def gmail_beta_enabled() -> bool:
    """Return True only when Gmail operational surfaces are explicitly enabled."""
    raw = (os.environ.get(_ENV_KEY) or "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def gmail_beta_frozen() -> bool:
    return not gmail_beta_enabled()


def gmail_beta_frozen_detail() -> str:
    return _GMAIL_BETA_FROZEN_DETAIL
