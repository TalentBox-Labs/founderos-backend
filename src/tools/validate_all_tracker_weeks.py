"""
Run the full validation pipeline (research_mapper → … → publish_checklist_checker)
once per `content_id` in `tracker.csv`, using `data/week_runtime/{ID}.json` as
`WORKCREW_RUNTIME_CONFIG` for each run.

Usage:
  python -m src.tools.validate_all_tracker_weeks

Exit code 0 only if every week passes. Does not run CrewAI or update the tracker.
On exit, clears ``WORKCREW_RUNTIME_CONFIG`` in this process so the last profile
does not leak into a subsequent command in the same shell.
"""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

from src.tools.pipeline_runner import run_validation_pipeline
from src.tools.runtime_paths import REPO_ROOT, TRACKER_PATH
from src.tools.sheet_sync import _week_id_sort_key
from src.tools.env_compat import set_compat, pop_compat

PROFILES = REPO_ROOT / "data" / "week_runtime"


def _tracker_rows() -> list[dict[str, str]]:
    with TRACKER_PATH.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def main() -> int:
    rows = sorted(_tracker_rows(), key=lambda r: _week_id_sort_key(r["content_id"]))
    failures: list[str] = []
    try:
        for row in rows:
            wid = str(row["content_id"]).strip()
            prof = PROFILES / f"{wid}.json"
            if not prof.is_file():
                failures.append(f"{wid}: missing profile {prof.relative_to(REPO_ROOT)}")
                continue
            set_compat("RUNTIME_CONFIG", str(prof.resolve()))
            print(f"\n{'='*60}\nVALIDATING {wid}  (profile {prof.name})\n{'='*60}")
            try:
                run_validation_pipeline()
            except RuntimeError as e:
                failures.append(f"{wid}: {e}")
    finally:
        # Always clear so the last validated week's profile does not leak into a
        # follow-on command in the same shell (e.g. main.py / pipeline_orchestrator).
        pop_compat("RUNTIME_CONFIG")

    if failures:
        print("\nVALIDATE_ALL FAILED:\n", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1

    print(f"\nAll {len(rows)} tracker week(s) passed validation gates.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
