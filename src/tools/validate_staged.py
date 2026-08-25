"""Run Phase 2A / 2B validator gates against staged paths (overlay runtime config)."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Any

from src.tools.env_compat import founder_os_key, workcrew_key
from src.tools.promotion_audit import parse_validator_verdict
from src.tools.runtime_paths import REPO_ROOT
from src.tools.staging_overlay import (
    PHASE2A_PROMOTION_VALIDATORS,
    PHASE2B_PROMOTION_VALIDATORS,
    write_phase2a_overlay_file,
    write_phase2b_overlay_file,
    write_phase3_overlay_file,
)

_VALIDATOR_MODULES_2A: dict[str, str] = {
    "research_mapper": "src.tools.research_mapper",
    "draft_validator": "src.tools.draft_validator",
}

_VALIDATOR_MODULES_2B: dict[str, str] = {
    "structure_checker": "src.tools.structure_checker",
    "metadata_checker": "src.tools.metadata_checker",
}

_VALIDATOR_MODULES_2C: dict[str, str] = {
    "content_quality": "src.tools.content_quality_checker",
}

PHASE2C_PROMOTION_VALIDATORS: tuple[str, ...] = ("content_quality",)

_VALIDATOR_MODULES_3: dict[str, str] = {
    "distribution_bundle": "src.tools.distribution_bundle_checker",
}

PHASE3_PROMOTION_VALIDATORS: tuple[str, ...] = ("distribution_bundle",)

_OUTPUT_CAP = 80_000


def _run_validator_chain(
    *,
    staging_root: str,
    week_id: str,
    validator_ids: tuple[str, ...],
    modules: dict[str, str],
    overlay_path_factory,
) -> tuple[int, list[dict[str, Any]]]:
    overlay = overlay_path_factory(staging_root.strip(), week_id.strip().upper())
    rel = overlay.relative_to(REPO_ROOT)
    env = os.environ.copy()
    env[founder_os_key("RUNTIME_CONFIG")] = str(rel)
    env[workcrew_key("RUNTIME_CONFIG")] = str(rel)

    runs: list[dict[str, Any]] = []
    failed = False

    for vid in validator_ids:
        module = modules[vid]
        print(f"\n=== Staged validation: {vid} ===\n", file=sys.stderr)
        proc = subprocess.run(
            [sys.executable, "-m", module],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        print(out)
        verdict = parse_validator_verdict(out)
        capped = out if len(out) <= _OUTPUT_CAP else out[:_OUTPUT_CAP]
        runs.append(
            {
                "validator_id": vid,
                "exit_code": proc.returncode,
                "verdict": verdict,
                "combined_output": capped,
                "output_truncated": len(out) > _OUTPUT_CAP,
                "output_bytes": len(out.encode("utf-8", errors="replace")),
            }
        )

        if proc.returncode != 0:
            print(f"FAIL: {vid} exit {proc.returncode}", file=sys.stderr)
            failed = True
        elif verdict == "FAIL":
            print(f"FAIL: {vid} reported FAIL verdict", file=sys.stderr)
            failed = True
        elif verdict == "UNKNOWN":
            print(f"WARNING: {vid} verdict UNKNOWN — check output", file=sys.stderr)

    if failed:
        print("\nStaged validation FAILED.\n", file=sys.stderr)
        return 1, runs
    print("\nStaged validation PASSED.\n", file=sys.stderr)
    return 0, runs


def run_phase2a_validators(
    staging_root: str,
    week_id: str,
) -> tuple[int, list[dict[str, Any]]]:
    """research_mapper + draft_validator."""
    return _run_validator_chain(
        staging_root=staging_root,
        week_id=week_id,
        validator_ids=PHASE2A_PROMOTION_VALIDATORS,
        modules=_VALIDATOR_MODULES_2A,
        overlay_path_factory=write_phase2a_overlay_file,
    )


def run_phase2b_validators(
    staging_root: str,
    week_id: str,
) -> tuple[int, list[dict[str, Any]]]:
    """structure_checker + metadata_checker on staged 05_Final.md."""
    return _run_validator_chain(
        staging_root=staging_root,
        week_id=week_id,
        validator_ids=PHASE2B_PROMOTION_VALIDATORS,
        modules=_VALIDATOR_MODULES_2B,
        overlay_path_factory=write_phase2b_overlay_file,
    )


def run_phase2c_validators(
    staging_root: str,
    week_id: str,
) -> tuple[int, list[dict[str, Any]]]:
    """Deterministic body quality score on staged 05_Final.md (same overlay paths as 2B)."""
    return _run_validator_chain(
        staging_root=staging_root,
        week_id=week_id,
        validator_ids=PHASE2C_PROMOTION_VALIDATORS,
        modules=_VALIDATOR_MODULES_2C,
        overlay_path_factory=write_phase2b_overlay_file,
    )


def run_phase3_validators(
    staging_root: str,
    week_id: str,
) -> tuple[int, list[dict[str, Any]]]:
    """distribution_bundle_checker on staged 06–09 (requires distribution_staging_root overlay)."""
    return _run_validator_chain(
        staging_root=staging_root,
        week_id=week_id,
        validator_ids=PHASE3_PROMOTION_VALIDATORS,
        modules=_VALIDATOR_MODULES_3,
        overlay_path_factory=write_phase3_overlay_file,
    )


def validate_staged_phase2a(staging_root: str, week_id: str) -> int:
    code, _ = run_phase2a_validators(staging_root, week_id)
    return code


def validate_staged_phase2b(staging_root: str, week_id: str) -> int:
    code, _ = run_phase2b_validators(staging_root, week_id)
    return code


def validate_staged_phase2c(staging_root: str, week_id: str) -> int:
    code, _ = run_phase2c_validators(staging_root, week_id)
    return code


def validate_staged_phase3(staging_root: str, week_id: str) -> int:
    code, _ = run_phase3_validators(staging_root, week_id)
    return code


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate staged artifacts with a temporary runtime overlay "
        "(2A: 01–04; 2B: 05_Final structure+metadata; 2C: body quality; 3: 06–09)."
    )
    parser.add_argument(
        "staging_root",
        help="Repo-relative directory (e.g. output/generated/W05).",
    )
    parser.add_argument(
        "--week",
        metavar="WXX",
        required=True,
        help="Week id matching tracker/runtime (e.g. W05).",
    )
    parser.add_argument(
        "--phase",
        choices=("2a", "2b", "2c", "3"),
        default="2a",
        help="2a = research + draft; 2b = structure + metadata; 2C = content quality; 3 = 06–09.",
    )
    args = parser.parse_args()
    if args.phase == "2b":
        rc = validate_staged_phase2b(args.staging_root, args.week)
    elif args.phase == "2c":
        rc = validate_staged_phase2c(args.staging_root, args.week)
    elif args.phase == "3":
        rc = validate_staged_phase3(args.staging_root, args.week)
    else:
        rc = validate_staged_phase2a(args.staging_root, args.week)
    sys.exit(rc)


if __name__ == "__main__":
    main()
