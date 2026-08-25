"""Promote staged artifacts into canonical input/WXX/ (Phase 2A or 2B) with audit trail."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.tools.env_compat import getenv_compat, getenv_compat_optional
from src.tools.promotion_audit import (
    AUDIT_DIR,
    PHASE2A_PROMOTION_FILENAMES,
    PHASE2B_PROMOTION_FILENAMES,
    PHASE3_PROMOTION_FILENAMES,
    compute_file_diffs,
    write_audit_bundle,
)
from src.tools.runtime_paths import REPO_ROOT
from src.tools.validate_staged import (
    run_phase2a_validators,
    run_phase2b_validators,
    run_phase3_validators,
)


def _canonical_week_dir(week_id: str) -> Path:
    return REPO_ROOT / "input" / week_id.upper()


def _backup_sources(
    staging: Path,
    canonical: Path,
    week_id: str,
    filenames: tuple[str, ...],
    *,
    ts_slug: str | None = None,
    backup_note: str = "",
) -> Path:
    ts = ts_slug or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = canonical / ".promotion_backup" / ts
    backup_root.mkdir(parents=True, exist_ok=True)
    meta = backup_root / "SOURCES.txt"
    lines = [
        f"week={week_id}",
        f"staging_dir={staging.relative_to(REPO_ROOT)}",
        "",
        backup_note or "Prior canonical copies of promoted files (if any) are saved beside this file.",
    ]
    meta.write_text("\n".join(lines) + "\n", encoding="utf-8")

    for name in filenames:
        prev = canonical / name
        if prev.is_file():
            shutil.copy2(prev, backup_root / name)
    return backup_root


def promote_staged_bundle(
    *,
    staging_root: str,
    week_id: str,
    phase: str,
    skip_validation: bool,
    dry_run: bool,
    approver: str | None,
    notes: str | None,
) -> int:
    """
    phase: '2a' promotes 01–04; '2b' promotes 05_Final.md only; '3' promotes 06–09 only.
    """
    wid = week_id.upper()
    staging = REPO_ROOT / staging_root.strip().rstrip("/")
    canonical = _canonical_week_dir(wid)

    if phase == "2a":
        files = PHASE2A_PROMOTION_FILENAMES
        event_name = "promotion_phase2a"
        phase_label = "Phase 2A (01–04)"
    elif phase == "2b":
        files = PHASE2B_PROMOTION_FILENAMES
        event_name = "promotion_phase2b"
        phase_label = "Phase 2B (05_Final)"
    else:
        files = PHASE3_PROMOTION_FILENAMES
        event_name = "promotion_phase3"
        phase_label = "Phase 3 (06–09 distribution)"

    missing = [n for n in files if not (staging / n).is_file()]
    if missing:
        print(f"ERROR: Staging dir missing files: {missing}", file=sys.stderr)
        return 1

    approver_resolved = (approver or getenv_compat_optional("PROMOTION_APPROVER") or "").strip()

    if dry_run:
        print(
            f"DRY RUN ({phase_label}): would "
            f"{'skip validation' if skip_validation else 'run validators'}, "
            f"record audit approver={approver_resolved or '[REQUIRED]'}, "
            f"then copy {list(files)} from {staging.relative_to(REPO_ROOT)} "
            f"-> {canonical.relative_to(REPO_ROOT)}",
            file=sys.stderr,
        )
        return 0

    if not approver_resolved:
        print(
            "ERROR: Promotion requires --approver NAME or WORKCREW_PROMOTION_APPROVER. "
            "See docs/Artifact_Promotion_PRD.md.",
            file=sys.stderr,
        )
        return 2

    event_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    validator_runs: list = []
    if not skip_validation:
        if phase == "2a":
            rc, validator_runs = run_phase2a_validators(staging_root, wid)
        elif phase == "2b":
            rc, validator_runs = run_phase2b_validators(staging_root, wid)
        else:
            rc, validator_runs = run_phase3_validators(staging_root, wid)
        if rc != 0:
            print(
                "Promotion aborted: staged artifacts failed validation.",
                file=sys.stderr,
            )
            return 1
    else:
        print(
            "WARNING: Skipping validation — violates normal governance.",
            file=sys.stderr,
        )
        validator_runs = [
            {
                "validator_id": "_gates_skipped",
                "verdict": "SKIPPED",
                "reason": "--skip-validation with WORKCREW_PROMOTE_FORCE",
            }
        ]

    audit_prefix = f"{event_ts}_{wid}"
    diff_dir = AUDIT_DIR / f"{audit_prefix}_diffs"
    diff_full, diff_artifacts = compute_file_diffs(
        staging,
        canonical,
        audit_prefix=audit_prefix,
        diff_out_dir=diff_dir,
        filenames=files,
    )

    if phase == "2b":
        backup_note = "Phase 2B: prior canonical 05_Final.md only."
    elif phase == "3":
        backup_note = "Phase 3: prior canonical 06–09 only."
    else:
        backup_note = "Phase 2A: prior canonical 01–04."
    canonical.mkdir(parents=True, exist_ok=True)
    backup_root = _backup_sources(
        staging,
        canonical,
        wid,
        files,
        ts_slug=event_ts,
        backup_note=backup_note,
    )
    backup_rel = str(backup_root.relative_to(REPO_ROOT))
    print(f"Backed up prior canonical files to {backup_rel}", file=sys.stderr)

    for name in files:
        shutil.copy2(staging / name, canonical / name)

    audit_path = write_audit_bundle(
        event_ts_slug=event_ts,
        week_id=wid,
        staging_root=staging_root,
        approver=approver_resolved,
        notes=notes or "",
        skip_validation=skip_validation,
        validator_runs=validator_runs,
        backup_relative=backup_rel,
        canonical_path=canonical,
        diff_artifacts=diff_artifacts,
        diff_character_counts={k: len(v) for k, v in diff_full.items()},
        event_name=event_name,
    )

    print(
        f"Promoted {wid} ({phase_label}). Audit: {audit_path.relative_to(REPO_ROOT)}",
        file=sys.stderr,
    )
    print(
        "Append-only history: output/promotion_audit/promotion_history.jsonl",
        file=sys.stderr,
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Promote staged artifacts into input/WXX/ after validation + audit."
    )
    parser.add_argument(
        "staging_root",
        help="Repo-relative staging directory (e.g. output/generated/W05).",
    )
    parser.add_argument("--week", metavar="WXX", required=True, help="Target week id.")
    g = parser.add_mutually_exclusive_group()
    g.add_argument(
        "--final-only",
        action="store_true",
        help="Phase 2B: promote only 05_Final.md (structure + metadata gates).",
    )
    g.add_argument(
        "--distribution-only",
        action="store_true",
        help="Phase 3: promote only 06–09 (distribution bundle gate).",
    )
    parser.add_argument(
        "--approver",
        help="Human who approved promotion (or WORKCREW_PROMOTION_APPROVER).",
    )
    parser.add_argument("--notes", default="", help="Optional audit notes.")
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Bypass gates (requires WORKCREW_PROMOTE_FORCE=1). Emergency only.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions only.",
    )
    args = parser.parse_args()

    skip_val = False
    if args.skip_validation:
        if getenv_compat("PROMOTE_FORCE", "").lower() not in ("1", "true", "yes"):
            print(
                "ERROR: --skip-validation requires WORKCREW_PROMOTE_FORCE=1.",
                file=sys.stderr,
            )
            sys.exit(2)
        skip_val = True

    if args.distribution_only:
        phase = "3"
    elif args.final_only:
        phase = "2b"
    else:
        phase = "2a"
    rc = promote_staged_bundle(
        staging_root=args.staging_root,
        week_id=args.week,
        phase=phase,
        skip_validation=skip_val,
        dry_run=args.dry_run,
        approver=args.approver,
        notes=args.notes,
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
