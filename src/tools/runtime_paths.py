"""Single source for repository root and runtime config paths (cwd-independent)."""

from __future__ import annotations

import json
from pathlib import Path

from src.tools.env_compat import getenv_compat_optional

# src/tools/runtime_paths.py -> parents[2] == repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_CONFIG_PATH = REPO_ROOT / "data" / "runtime_config.json"
TRACKER_PATH = REPO_ROOT / "tracker.csv"


def load_canonical_runtime_config() -> dict:
    """Always reads `data/runtime_config.json` (ignores FOUNDER_OS_/WORKCREW_ RUNTIME_CONFIG)."""
    if not RUNTIME_CONFIG_PATH.is_file():
        raise FileNotFoundError(f"Runtime config not found: {RUNTIME_CONFIG_PATH}")
    return json.loads(RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))


def load_runtime_config() -> dict:
    """
    Loads runtime JSON. If env ``FOUNDER_OS_RUNTIME_CONFIG`` (preferred) or legacy
    ``WORKCREW_RUNTIME_CONFIG`` is set to an absolute or repo-relative path, that file
    is used instead — for staging overlays during validation/promotion dry-runs.
    """
    override = getenv_compat_optional("RUNTIME_CONFIG")
    if override:
        path = Path(override)
        if not path.is_absolute():
            path = REPO_ROOT / path
        if not path.is_file():
            raise FileNotFoundError(f"Runtime config override not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))
    return load_canonical_runtime_config()


def checklist_path_for_week(week_id: str) -> Path:
    return REPO_ROOT / "input" / week_id / "09_Publish_Checklist.md"
