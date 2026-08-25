"""Re-export Phase 1 env compatibility for revenue_os consumers."""

from __future__ import annotations

from src.tools.env_compat import (
    WORKCREW_ENV_SUFFIXES,
    founder_os_key,
    getenv_compat,
    getenv_compat_optional,
    pop_compat,
    set_compat,
    workcrew_key,
)

__all__ = [
    "WORKCREW_ENV_SUFFIXES",
    "founder_os_key",
    "getenv_compat",
    "getenv_compat_optional",
    "pop_compat",
    "set_compat",
    "workcrew_key",
]
