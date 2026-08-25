"""
Copy Repo mirror rows → reader tab (e.g. Content Tracker) by week ID in column A.

Same behaviour as scripts/sheets_copy_mirror_values.gs, runnable locally with the
service account (no Apps Script UI). Run after sheet_sync --write so the reader
tab matches the mirror.

Env:
  WORKCREW_GOOGLE_SERVICE_ACCOUNT, WORKCREW_GOOGLE_SHEET_ID,
  WORKCREW_GOOGLE_SHEET_HEADER_ROW — same as sheet_sync.
  WORKCREW_GOOGLE_SHEET_TAB — source tab (default use: Repo mirror).
  WORKCREW_GOOGLE_READER_TAB — target tab (default: Content Tracker). Must match the
  **exact** sheet tab title you open in the browser (e.g. ``Contract Tracker``).

  WORKCREW_READER_SYNC_VERIFY=1 — after loading the mirror, compare ``Status`` and
  ``Published URL`` to values composed from ``tracker.csv`` + repo; print warnings
  when the mirror is stale (often because ``sheet_sync`` skipped formula cells on
  **Repo mirror** — clear formulas in those cells and re-run ``sheet_sync --write``).
"""

from __future__ import annotations

import csv
import os
import sys

from src.tools.google_sheet_sync import (
    SheetEnv,
    a1_escape_tab,
    batch_update_cells,
    build_sheets_service,
    col_index_to_letters,
    fetch_id_to_row,
    load_sheet_env,
    load_sa_email,
)
from src.tools.runtime_paths import REPO_ROOT, TRACKER_PATH
from src.tools.sheet_sync import SHEET_HEADERS, compose_sheet_row
from src.tools.env_compat import getenv_compat

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

NUM_COLS = len(SHEET_HEADERS)

_VERIFY_KEYS = ("Status", "Published URL")


def _row_cells_to_dict(cells: list[str]) -> dict[str, str]:
    return {
        SHEET_HEADERS[i]: (cells[i] if i < len(cells) else "") for i in range(NUM_COLS)
    }


def _load_tracker_rows_by_id() -> dict[str, dict[str, str]]:
    if not TRACKER_PATH.is_file():
        return {}
    with TRACKER_PATH.open(encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        return {(row.get("content_id") or "").strip().upper(): row for row in r}


def _warn_if_mirror_stale_vs_repo(
    mirror_by_id: dict[str, list[str]],
) -> None:
    """Compare key columns to repo truth; mirror is wrong if sheet_sync skipped formulas."""
    by_id = _load_tracker_rows_by_id()
    for wid, cells in sorted(mirror_by_id.items()):
        tr = by_id.get(wid)
        if not tr:
            continue
        try:
            expected = compose_sheet_row(tr, REPO_ROOT)
        except OSError:
            continue
        got = _row_cells_to_dict(cells)
        for k in _VERIFY_KEYS:
            ev = str(expected.get(k, "")).strip()
            gv = str(got.get(k, "")).strip()
            if ev != gv:
                print(
                    f"WARNING: Repo mirror {wid} column {k!r}: sheet has {gv!r} "
                    f"but repo composes {ev!r}. "
                    f"If {k!r} is a formula on **Repo mirror**, `sheet_sync --write` "
                    f"skips that cell — paste values-only or clear the formula, then re-run "
                    f"`sheet_sync --write` and this reader sync.",
                    file=sys.stderr,
                )


def _pad_row(cells: list[str]) -> list[str]:
    out = ["" if c is None else str(c) for c in cells[:NUM_COLS]]
    while len(out) < NUM_COLS:
        out.append("")
    return out


def fetch_mirror_rows_by_id(
    service,
    source: SheetEnv,
) -> dict[str, list[str]]:
    """ID → full row A–V display strings from source tab."""
    tab = a1_escape_tab(source.tab_title)
    start = source.data_start_row_1based
    last_letter = col_index_to_letters(NUM_COLS - 1)
    end = start + 5000
    rng = f"{tab}!A{start}:{last_letter}{end}"
    res = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=source.spreadsheet_id,
            range=rng,
            valueRenderOption="FORMATTED_VALUE",
        )
        .execute()
    )
    rows = res.get("values") or []
    out: dict[str, list[str]] = {}
    for row in rows:
        if not row:
            continue
        rid = str(row[0]).strip() if row[0] is not None else ""
        if not rid:
            continue
        out[rid] = _pad_row([str(c) if c is not None else "" for c in row])
    return out


def run_reader_sync() -> int:
    reader_title = getenv_compat("GOOGLE_READER_TAB", "Content Tracker").strip()
    if not reader_title:
        print("ERROR: WORKCREW_GOOGLE_READER_TAB is empty.", file=sys.stderr)
        return 1

    source_env = load_sheet_env(require_tab=True)
    print(
        f"reader_tab_sync: source tab={source_env.tab_title!r} → "
        f"reader tab={reader_title!r} "
        f"(set WORKCREW_GOOGLE_READER_TAB to the exact tab name you use in Sheets).",
        file=sys.stderr,
    )
    sa_path = source_env.sa_path
    target_env = SheetEnv(
        sa_path=sa_path,
        spreadsheet_id=source_env.spreadsheet_id,
        tab_title=reader_title,
        header_row_1based=source_env.header_row_1based,
    )

    try:
        service = build_sheets_service(sa_path, readonly=False)
        mirror_by_id = fetch_mirror_rows_by_id(service, source_env)
        if getenv_compat("READER_SYNC_VERIFY", "").strip() in (
            "1",
            "true",
            "yes",
        ):
            _warn_if_mirror_stale_vs_repo(mirror_by_id)
        dst_by_id = fetch_id_to_row(service, target_env)
    except Exception as e:
        print(f"ERROR: Google Sheets API failed: {e}", file=sys.stderr)
        try:
            hint = load_sa_email(sa_path)
        except Exception:
            hint = "(set WORKCREW_GOOGLE_SERVICE_ACCOUNT)"
        print(
            f"Hint: Share the spreadsheet with the service account (Editor):\n  {hint}",
            file=sys.stderr,
        )
        return 1

    tab_a1 = a1_escape_tab(target_env.tab_title)
    last_l = col_index_to_letters(NUM_COLS - 1)
    batch: list[tuple[str, list[list[str]]]] = []
    updated = 0
    skipped = 0
    for wid, vals in mirror_by_id.items():
        dest_row = dst_by_id.get(wid)
        if not dest_row:
            skipped += 1
            continue
        rng = f"{tab_a1}!A{dest_row}:{last_l}{dest_row}"
        batch.append((rng, [vals]))
        updated += 1

    if batch:
        batch_update_cells(service, source_env.spreadsheet_id, batch)

    print(
        f"Reader tab sync: source={source_env.tab_title!r} → target={reader_title!r}; "
        f"updated={updated}; mirror ids with no reader row (column A)={skipped}",
        file=sys.stderr,
    )
    return 0


def main() -> None:
    raise SystemExit(run_reader_sync())


if __name__ == "__main__":
    main()
