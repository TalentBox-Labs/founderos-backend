"""
Google Sheets helpers for Phase 4B — dry-run validation and controlled writes.

Does not load runtime_config for mutation; callers pass repo_root / paths only.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from pathlib import Path

from src.tools.runtime_paths import REPO_ROOT
from src.tools.env_compat import getenv_compat

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

SCOPES_READWRITE = ("https://www.googleapis.com/auth/spreadsheets",)


def a1_escape_tab(title: str) -> str:
    """Quote sheet tab for A1 notation if needed."""
    if re.search(r"[^\w]", title):
        return "'" + title.replace("'", "''") + "'"
    return title


def col_index_to_letters(idx_zero_based: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    n = idx_zero_based + 1
    letters = ""
    while n:
        n, r = divmod(n - 1, 26)
        letters = chr(65 + r) + letters
    return letters


@dataclass(frozen=True)
class SheetEnv:
    sa_path: Path
    spreadsheet_id: str
    tab_title: str
    header_row_1based: int

    @property
    def data_start_row_1based(self) -> int:
        return self.header_row_1based + 1


def load_sheet_env(*, require_tab: bool) -> SheetEnv:
    sa_raw = getenv_compat("GOOGLE_SERVICE_ACCOUNT", "").strip()
    sheet_id = getenv_compat("GOOGLE_SHEET_ID", "").strip()
    tab = getenv_compat("GOOGLE_SHEET_TAB", "").strip()
    hr_raw = getenv_compat("GOOGLE_SHEET_HEADER_ROW", "").strip()

    if not sa_raw:
        raise RuntimeError(
            "Set WORKCREW_GOOGLE_SERVICE_ACCOUNT to the absolute path of the service account JSON."
        )
    if not sheet_id:
        raise RuntimeError("Set WORKCREW_GOOGLE_SHEET_ID.")
    if require_tab and not tab:
        raise RuntimeError(
            "Set WORKCREW_GOOGLE_SHEET_TAB to the exact tab title (e.g. Content Tracker)."
        )

    sa_path = Path(sa_raw).expanduser()
    if not sa_path.is_file():
        raise RuntimeError(f"Service account file not found: {sa_path}")

    header_row = int(hr_raw) if hr_raw else 3
    if header_row < 1:
        raise RuntimeError("WORKCREW_GOOGLE_SHEET_HEADER_ROW must be >= 1.")

    return SheetEnv(
        sa_path=sa_path,
        spreadsheet_id=sheet_id,
        tab_title=tab or "Sheet1",
        header_row_1based=header_row,
    )


def build_sheets_service(sa_path: Path, *, readonly: bool):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    scopes = (
        ("https://www.googleapis.com/auth/spreadsheets.readonly",)
        if readonly
        else SCOPES_READWRITE
    )
    creds = service_account.Credentials.from_service_account_file(
        str(sa_path), scopes=scopes
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def fetch_sheet_headers(
    service: Any,
    env: SheetEnv,
    *,
    num_cols: int,
) -> list[str]:
    """Reads one header row A..num_cols on the configured tab."""
    tab = a1_escape_tab(env.tab_title)
    last_letter = col_index_to_letters(num_cols - 1)
    rng = f"{tab}!A{env.header_row_1based}:{last_letter}{env.header_row_1based}"
    res = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=env.spreadsheet_id, range=rng)
        .execute()
    )
    rows = res.get("values") or []
    if not rows:
        return []
    raw = rows[0][:num_cols]
    cells = [str(x).strip() if x is not None else "" for x in raw]
    while len(cells) < num_cols:
        cells.append("")
    return cells[:num_cols]


def fetch_id_to_row(
    service: Any,
    env: SheetEnv,
    *,
    max_rows: int = 5000,
) -> dict[str, int]:
    """Maps ID column (A) cell text -> 1-based sheet row for existing data rows."""
    tab = a1_escape_tab(env.tab_title)
    start = env.data_start_row_1based
    end = start + max_rows
    rng = f"{tab}!A{start}:A{end}"
    res = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=env.spreadsheet_id, range=rng)
        .execute()
    )
    values = res.get("values") or []
    out: dict[str, int] = {}
    for i, row in enumerate(values):
        if not row:
            continue
        cell = str(row[0]).strip()
        if cell:
            out[cell] = start + i
    return out


def fetch_row_display(
    service: Any,
    env: SheetEnv,
    row_1based: int,
    *,
    num_cols: int,
    value_render_option: str,
) -> list[str]:
    tab = a1_escape_tab(env.tab_title)
    last = col_index_to_letters(num_cols - 1)
    rng = f"{tab}!A{row_1based}:{last}{row_1based}"
    res = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=env.spreadsheet_id,
            range=rng,
            valueRenderOption=value_render_option,
        )
        .execute()
    )
    rows = res.get("values") or []
    if not rows:
        return [""] * num_cols
    cells = rows[0][:num_cols]
    pad = num_cols - len(cells)
    return ["" if c is None else str(c) for c in cells] + [""] * pad


def next_blank_row_after_ids(
    service: Any,
    env: SheetEnv,
    *,
    max_rows: int = 5000,
) -> int:
    """First 1-based row at or below data_start with empty column A after last ID."""
    tab = a1_escape_tab(env.tab_title)
    start = env.data_start_row_1based
    end = start + max_rows
    rng = f"{tab}!A{start}:A{end}"
    res = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=env.spreadsheet_id, range=rng)
        .execute()
    )
    values = res.get("values") or []
    last_filled = start - 1
    for i, row in enumerate(values):
        if row and str(row[0]).strip():
            last_filled = start + i
    return last_filled + 1


def batch_update_cells(
    service: Any,
    spreadsheet_id: str,
    ranges_values: list[tuple[str, list[list[Any]]]],
) -> None:
    """Writes multiple disjoint ranges in one batchUpdate (USER_ENTERED)."""
    if not ranges_values:
        return
    body: dict[str, Any] = {
        "valueInputOption": "USER_ENTERED",
        "data": [{"range": r, "values": v} for r, v in ranges_values],
    }
    (
        service.spreadsheets()
        .values()
        .batchUpdate(spreadsheetId=spreadsheet_id, body=body)
        .execute()
    )


def load_sa_email(sa_path: Path) -> str:
    data = json.loads(sa_path.read_text(encoding="utf-8"))
    email = data.get("client_email")
    if not email:
        raise ValueError("JSON missing client_email")
    return str(email)


def get_sheet_id_for_tab_title(
    service: Any, spreadsheet_id: str, tab_title: str
) -> int:
    """Resolve numeric sheetId for a tab title (batchUpdate moveDimension)."""
    spreadsheet = (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets(properties(sheetId,title))")
        .execute()
    )
    for sheet in spreadsheet.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == tab_title:
            return int(props["sheetId"])
    raise ValueError(f"No sheet tab titled {tab_title!r}")


def delete_sheet_rows_1based(
    service: Any,
    spreadsheet_id: str,
    sheet_id: int,
    row_numbers_1based: list[int],
) -> None:
    """
    Delete entire grid rows by 1-based row number. Removes bottom rows first so
    indices remain valid for subsequent deletes.
    """
    unique = sorted({r for r in row_numbers_1based if r >= 1}, reverse=True)
    if not unique:
        return
    requests: list[dict[str, Any]] = []
    for r in unique:
        si = r - 1
        ei = r
        requests.append(
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": si,
                        "endIndex": ei,
                    }
                }
            }
        )
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests},
    ).execute()


def move_single_row_to_before_row(
    service: Any,
    spreadsheet_id: str,
    sheet_id: int,
    source_row_1based: int,
    destination_row_1based: int,
) -> None:
    """
    Move one spreadsheet row so it occupies destination_row_1based after the move.

    Uses MoveDimensionRequest (0-based grid indices). Single-row source must not
    overlap destination (handled when source != destination).
    """
    if source_row_1based == destination_row_1based:
        return
    si = source_row_1based - 1
    ei = source_row_1based
    di = destination_row_1based - 1
    body: dict[str, Any] = {
        "requests": [
            {
                "moveDimension": {
                    "source": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": si,
                        "endIndex": ei,
                    },
                    "destinationIndex": di,
                }
            }
        ]
    }
    service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=body).execute()


def reorder_data_rows_to_composed_order(
    service: Any,
    env: SheetEnv,
    composed: list[dict[str, str]],
) -> list[str]:
    """
    Physically reorder sheet rows so column A from data_start downward matches
    composed row order (e.g. W01, W02, … by numeric week id).

    Returns human-readable move log lines for audits.
    """
    if len(composed) < 2:
        return []

    desired = [r["ID"] for r in composed]
    data_start = env.data_start_row_1based
    sheet_id = get_sheet_id_for_tab_title(service, env.spreadsheet_id, env.tab_title)

    log: list[str] = []
    max_passes = len(composed) ** 2 + len(composed) + 5

    for _ in range(max_passes):
        id_to_row = fetch_id_to_row(service, env)
        present = [(id_to_row[w], w) for w in desired if w in id_to_row]
        if len(present) != len(desired):
            missing = [w for w in desired if w not in id_to_row]
            raise RuntimeError(
                "Cannot reorder: tracker ids missing from sheet column A: "
                + ", ".join(missing)
            )
        ordered_by_sheet = [w for _, w in sorted(present, key=lambda x: x[0])]
        if ordered_by_sheet == desired:
            return log

        for pos in range(len(desired)):
            want = desired[pos]
            if pos >= len(ordered_by_sheet) or ordered_by_sheet[pos] != want:
                cur_row = id_to_row[want]
                target_row = data_start + pos
                if cur_row != target_row:
                    move_single_row_to_before_row(
                        service,
                        env.spreadsheet_id,
                        sheet_id,
                        cur_row,
                        target_row,
                    )
                    log.append(f"{want}: sheet row {cur_row} -> row {target_row}")
                break
    raise RuntimeError(
        f"Reorder did not converge; want order {desired}, still have {ordered_by_sheet}"
    )
