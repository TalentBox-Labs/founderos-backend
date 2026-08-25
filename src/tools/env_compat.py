"""Founder OS / WorkCrew environment compatibility (Phase 1).

Canonical machine env prefix: FOUNDER_OS_*
Legacy compatibility prefix: WORKCREW_* (deprecated, not removed)

Resolution order:
1. FOUNDER_OS_<SUFFIX> if the key is present in the process environment
2. WORKCREW_<SUFFIX> if present
3. explicit default

Canonical Founder OS wins when both are present.
Never log secret values from this module.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping

# Suffixes that currently have active WORKCREW_* readers (Phase 1 inventory).
WORKCREW_ENV_SUFFIXES: tuple[str, ...] = (
    "ALLOW_DIRECT_INPUT_WRITE",
    "CREWAI_API_KEY",
    "CREWAI_MODEL",
    "CREWAI_TEMPERATURE",
    "GENERATION_BACKUP",
    "GOOGLE_READER_TAB",
    "GOOGLE_SERVICE_ACCOUNT",
    "GOOGLE_SHEET_HEADER_ROW",
    "GOOGLE_SHEET_ID",
    "GOOGLE_SHEET_TAB",
    "OLLAMA_BASE_URL",
    "PIPELINE_SUMMARY_JSON",
    "PROMOTE_FORCE",
    "PROMOTION_APPROVER",
    "READER_SYNC_VERIFY",
    "RUNTIME_CONFIG",
    "SHEET_SYNC_ROW_DELAY_S",
)


def founder_os_key(suffix: str) -> str:
    return f"FOUNDER_OS_{suffix}"


def workcrew_key(suffix: str) -> str:
    return f"WORKCREW_{suffix}"


def getenv_compat(
    suffix: str,
    default: str = "",
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve FOUNDER_OS_<suffix> with WORKCREW_<suffix> fallback.

    Presence of the FOUNDER_OS key (even empty string) wins over WORKCREW.
    """
    env = os.environ if environ is None else environ
    canonical = founder_os_key(suffix)
    legacy = workcrew_key(suffix)
    if canonical in env:
        return env[canonical]
    if legacy in env:
        return env[legacy]
    return default


def getenv_compat_optional(
    suffix: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Like getenv_compat, but returns None when neither key is present."""
    env = os.environ if environ is None else environ
    canonical = founder_os_key(suffix)
    legacy = workcrew_key(suffix)
    if canonical in env:
        return env[canonical]
    if legacy in env:
        return env[legacy]
    return None


def set_compat(
    suffix: str,
    value: str,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Write both FOUNDER_OS_* (canonical) and WORKCREW_* (legacy) for writers.

    Phase 1 keeps legacy writers/readers working while establishing the
    Founder OS key as the preferred surface.
    """
    env: MutableMapping[str, str] = os.environ if environ is None else environ
    env[founder_os_key(suffix)] = value
    env[workcrew_key(suffix)] = value


def pop_compat(
    suffix: str,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Remove both FOUNDER_OS_* and WORKCREW_* for a suffix."""
    env: MutableMapping[str, str] = os.environ if environ is None else environ
    env.pop(founder_os_key(suffix), None)
    env.pop(workcrew_key(suffix), None)
