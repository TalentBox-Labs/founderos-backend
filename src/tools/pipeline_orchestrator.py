"""External scheduler entry (cron, CI): run full pipeline + emit JSON summary.

Run from repo root:

    ./venv/bin/python -m src.tools.pipeline_orchestrator

Writes ``output/pipeline_orchestrator_run.json`` and prints one line prefixed with
``FOUNDER_OS_PIPELINE_SUMMARY_JSON=`` (preferred) plus legacy
``WORKCREW_PIPELINE_SUMMARY_JSON=`` for log capture.
Exit code ``0`` on success, ``1`` if ``run_pipeline()`` raised.

If your shell has ``FOUNDER_OS_RUNTIME_CONFIG`` or legacy ``WORKCREW_RUNTIME_CONFIG``
exported (e.g. after experiments), run
``env -u FOUNDER_OS_RUNTIME_CONFIG -u WORKCREW_RUNTIME_CONFIG python -m src.tools.pipeline_orchestrator`` so
``data/runtime_config.json`` is used.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone

from src.tools.pipeline_runner import run_pipeline
from src.tools.runtime_paths import REPO_ROOT, load_runtime_config
from src.tools.env_compat import getenv_compat_optional

SUMMARY_REL = "output/pipeline_orchestrator_run.json"


def run_with_summary() -> dict:
    override = getenv_compat_optional("RUNTIME_CONFIG")
    if override:
        print(
            f"\nNOTE: FOUNDER_OS_RUNTIME_CONFIG / WORKCREW_RUNTIME_CONFIG is set ({override!r}); "
            f"active week comes from that file, not data/runtime_config.json.\n"
            f"Unset it in this shell to use the canonical runtime (e.g. "
            f"`unset FOUNDER_OS_RUNTIME_CONFIG WORKCREW_RUNTIME_CONFIG` or "
            f"`env -u FOUNDER_OS_RUNTIME_CONFIG -u WORKCREW_RUNTIME_CONFIG …`).\n",
            file=sys.stderr,
            flush=True,
        )
    cfg = load_runtime_config()
    week = str(cfg.get("active_week", ""))
    started = datetime.now(timezone.utc).isoformat()
    rec: dict = {"active_week": week, "started_at": started, "ok": False}
    try:
        run_pipeline()
        rec["ok"] = True
    except Exception as e:
        rec["error"] = str(e)
        rec["traceback"] = traceback.format_exc()
    rec["finished_at"] = datetime.now(timezone.utc).isoformat()

    out_path = REPO_ROOT / SUMMARY_REL
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rec, indent=2), encoding="utf-8")

    print(
        "FOUNDER_OS_PIPELINE_SUMMARY_JSON=" + json.dumps(rec, separators=(",", ":")),
        flush=True,
    )
    # Legacy log-capture token preserved for existing schedulers/parsers.
    print(
        "WORKCREW_PIPELINE_SUMMARY_JSON=" + json.dumps(rec, separators=(",", ":")),
        flush=True,
    )
    return rec


def main() -> None:
    rec = run_with_summary()
    sys.exit(0 if rec.get("ok") else 1)


if __name__ == "__main__":
    main()
