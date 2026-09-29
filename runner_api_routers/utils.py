"""Shared utilities for runner_api routers."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from revenue_os.services.content_ops_authority import require_content_ops_access

# Constants
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PIPELINE_TIMEOUT_SEC = int(os.environ.get("RUNNER_PIPELINE_TIMEOUT_SEC", "1800"))
TAIL_CHARS = int(os.environ.get("RUNNER_LOG_TAIL_CHARS", "4000"))

# Security
security = HTTPBearer(auto_error=False)


def _get_runner_api_key() -> str:
    """Read on each auth check so tests can monkeypatch ``RUNNER_API_KEY``."""
    return os.environ.get("RUNNER_API_KEY", "").strip()


async def _verify_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """Fail-closed SERVICE API key auth, with HUMAN session accepted.

    - HUMAN cookie session → allowed (browser Content Ops / CRM / integrations)
    - Valid ``RUNNER_API_KEY`` Bearer → allowed (SERVICE automation)
    - Missing/empty ``RUNNER_API_KEY`` with no HUMAN session → 401 (no soft-open)
    - Invalid Bearer → 401

    Never elevates SERVICE to HUMAN/tenant authority; tenant resolution remains
    separate and HUMAN+membership based.
    """
    import hmac

    from revenue_os.services.identity_context import PrincipalKind
    from runner_api_routers.identity import identity_from_request

    ctx = identity_from_request(request)
    if (
        ctx is not None
        and ctx.principal_kind is PrincipalKind.HUMAN
        and ctx.is_human
        and ctx.user_id
    ):
        return "human-session"

    expected = _get_runner_api_key()
    if not expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not credentials:
        raise HTTPException(status_code=401, detail="Unauthorized")

    provided = credentials.credentials
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")

    return provided


async def require_human_or_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """Content Ops gate: HUMAN beta-tenant only.

    A valid ``RUNNER_API_KEY`` is SERVICE authentication for other surfaces.
    It is not human authority and not tenant authority here. ``credentials``
    is accepted so existing dependency overrides keep the same signature; the
    bearer value is never returned and never treated as an approver.
    """
    del credentials
    return require_content_ops_access(request)


def _validate_week_id(week_id: str) -> None:
    """Validate week_id format to prevent path traversal / injection.

    Raises ``ValueError`` on unsafe format. Membership in the content tracker is
    a separate concern (callers check ``_read_tracker()``).
    """
    if not week_id or not isinstance(week_id, str):
        raise ValueError("Invalid week ID format")
    if (
        "/" in week_id
        or "\\" in week_id
        or week_id.startswith(".")
        or any(ch in week_id for ch in ";|&`$<>(){}[]!")
    ):
        raise ValueError("Invalid week ID format")


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run subprocess command with timeout and capture output."""
    return subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=PIPELINE_TIMEOUT_SEC,
    )


def _tail(text: str) -> str:
    """Return last TAIL_CHARS characters of text."""
    if len(text) <= TAIL_CHARS:
        return text
    return text[-TAIL_CHARS:]


def _read_tracker() -> list[dict[str, str]]:
    """Read tracker.csv and return list of rows."""
    import csv

    tracker = PROJECT_ROOT / "tracker.csv"
    if not tracker.is_file():
        return []
    with tracker.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _load_runtime() -> dict[str, Any]:
    """Load runtime_config.json."""
    import json

    rc = PROJECT_ROOT / "data" / "runtime_config.json"
    if not rc.is_file():
        return {}
    return json.loads(rc.read_text(encoding="utf-8"))


def _last_run_summary() -> dict[str, Any] | None:
    """Load last pipeline_orchestrator_run.json."""
    import json

    p = PROJECT_ROOT / "output" / "pipeline_orchestrator_run.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _week_artifacts(week_id: str) -> dict[str, bool]:
    """Check which artifacts exist for a week.

    Path-safety only — does not 404 on unknown weeks. Calendar enrichment calls
    this for every tracker row. Tracker membership must be enforced by callers
    (e.g. ``page_week_detail`` / ``page_file_view`` via ``_read_tracker()``).
    """
    if not week_id or "/" in week_id or "\\" in week_id or week_id.startswith("."):
        return {}
    base = PROJECT_ROOT / "input" / week_id
    pipeline_steps = [
        ("Brief", "01_Content_Brief.md", "brief"),
        ("SEO Plan", "02_SEO_Plan.md", "seo"),
        ("Research", "03_Research.md", "research"),
        ("Draft", "04_Draft.md", "draft"),
        ("Final", "05_Final.md", "final"),
        ("Design", "06_Design_Brief.md", "design"),
        ("Social", "07_Social_Posts.md", "social"),
        ("Email", "08_Email_Copy.md", "email"),
        ("Checklist", "09_Publish_Checklist.md", "checklist"),
    ]
    return {key: (base / fname).is_file() for _, fname, key in pipeline_steps}


def _apply_week_if_set(week: str | None) -> tuple[bool, list[dict[str, Any]]]:
    """Apply week profile if week is specified. Returns (success, steps_log)."""
    steps: list[dict[str, Any]] = []
    if not week:
        return True, steps
    week = week.strip().upper()
    r = _run([sys.executable, "-m", "src.tools.runtime_apply", week])
    steps.append(
        {
            "step": "runtime_apply",
            "returncode": r.returncode,
            "stdout": _tail(r.stdout or ""),
            "stderr": _tail(r.stderr or ""),
        }
    )
    return r.returncode == 0, steps
