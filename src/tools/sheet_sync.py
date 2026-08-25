"""
Phase 4A/4B — Google Sheet mirror.

- Default / `--local-only`: validates tracker.csv, composes 22-column rows, prints CSV (no Google API).
- `--dry-run`: 4B.1 — local validation + sheet tab/header checks + planned upserts + audit JSON (requires `.env`).
- `--write`: 4B.2/4B.3 — repo-authoritative upsert into approved columns only; skips formulas & analytics columns;
  appends new IDs at the bottom when first inserted, then **reorders data rows** to match tracker order
  (`W01`, `W02`, `W05`, `W05A`, `W05B`, `W06A`, … including split week suffixes).

Does not read or write `data/runtime_config.json` (no runtime mutation).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.tools.google_sheet_sync import (
    SheetEnv,
    a1_escape_tab,
    batch_update_cells,
    build_sheets_service,
    col_index_to_letters,
    fetch_id_to_row,
    fetch_row_display,
    fetch_sheet_headers,
    load_sa_email,
    load_sheet_env,
    next_blank_row_after_ids,
    reorder_data_rows_to_composed_order,
)
from src.tools.metadata_checker import _split_front_matter
from src.tools.runtime_paths import REPO_ROOT, TRACKER_PATH, load_canonical_runtime_config
from src.tools.env_compat import getenv_compat

TRACKER_REQUIRED_COLUMNS: tuple[str, ...] = (
    "content_id",
    "title",
    "status",
    "qa_status",
    "current_step",
    "next_step",
    "draft_path",
    "qa_output_path",
    "final_output_path",
)

# Optional CSV columns (not required by validate_tracker_columns):
# - artifact_folder: when set (e.g. W05), path checks use input/{artifact_folder}/ instead of
#   input/{content_id}/ so sheet IDs like W05A/W05B can share one bundle until split on disk.

# Stakeholder sheet column order (22) — must match Phase4 PRD.
SHEET_HEADERS: tuple[str, ...] = (
    "ID",
    "Month/Week",
    "Publish Date",
    "Title",
    "Target Keyword",
    "Word Count",
    "Brief Done",
    "SEO Plan",
    "Research Done",
    "Draft Done",
    "Edited",
    "SEO Final",
    "Assets Done",
    "Email Ready",
    "Social Ready",
    "CTA",
    "Published URL",
    "Status",
    "Day7 Impr.",
    "Day30 Impr.",
    "Day30 Clicks",
    "Notes",
)

# Governance: never overwrite stakeholder / analytics columns from repo data.
SHEET_ANALYTICS_COLUMNS: frozenset[str] = frozenset(
    {"Day7 Impr.", "Day30 Impr.", "Day30 Clicks"}
)
# Columns the sync may write (repo + derived); analytics excluded.
AUTHORITATIVE_SYNC_HEADERS: tuple[str, ...] = tuple(
    h for h in SHEET_HEADERS if h not in SHEET_ANALYTICS_COLUMNS
)
SYNC_ROW_KEY_HEADER = "ID"  # maps from tracker `content_id` / week_id

_MIN_DISTRIBUTION_BYTES = 80


def _sheet_sync_row_delay_s() -> float:
    """
    Seconds to sleep after each row write that hits the Sheets API.

    Each update does several reads per row; bursty loops can hit
    ``Read requests per minute per user`` (HTTP 429). Default 1.2s keeps ~50
    row-mutations per minute under typical quotas. Set ``WORKCREW_SHEET_SYNC_ROW_DELAY_S``
    to ``0`` to disable (not recommended if you see 429).
    """
    raw = getenv_compat("SHEET_SYNC_ROW_DELAY_S", "1.2").strip()
    try:
        v = float(raw)
    except ValueError:
        return 1.2
    return max(0.0, min(v, 30.0))


def _tracker_cell(v) -> str:
    """Normalize tracker CSV cell (pandas may emit NaN for empty cells)."""
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except TypeError:
        pass
    return str(v).strip()


def _week_id_sort_key(content_id: str) -> tuple[int, int, str]:
    """
    Sort week rows for mirror + local CSV: W01, W02, W05, W05A, W05B, W06A, …, W09B, W10.

    Rules:
    - Primary: numeric week from `W{digits}` prefix.
    - Secondary: optional suffix letters `A`, `B`, … after the digits (split articles).
      Same week: empty suffix (e.g. plain `W05`) sorts before `W05A` before `W05B`.
    - Unknown ids sort last (stable by string).
    """
    wid = _tracker_cell(content_id).upper()
    m = re.match(r"^W(\d+)([A-Z]*)$", wid)
    if not m:
        return (10**9, 10**9, wid)
    week_num = int(m.group(1))
    suf = m.group(2)
    if not suf:
        suf_rank = 0
    elif len(suf) == 1 and "A" <= suf <= "Z":
        suf_rank = 1 + ord(suf) - ord("A")
    else:
        suf_rank = 100 + sum((i + 1) * ord(c) for i, c in enumerate(suf))
    return (week_num, suf_rank, wid)


def _bundle_wid_for_paths(tracker_row: dict, display_wid: str) -> str:
    """Folder under input/ for artifacts when `artifact_folder` column is set."""
    alt = _tracker_cell(tracker_row.get("artifact_folder"))
    return alt.upper() if alt else display_wid


def _strip_scalar(raw: str) -> str:
    s = raw.strip()
    if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        return s[1:-1].strip()
    return s


def _exists(repo_root: Path, rel: str) -> bool:
    p = repo_root / rel
    return p.is_file()


def _nontrivial(repo_root: Path, rel: str, min_bytes: int = _MIN_DISTRIBUTION_BYTES) -> bool:
    p = repo_root / rel
    if not p.is_file():
        return False
    try:
        return len(p.read_text(encoding="utf-8").strip()) >= min_bytes
    except OSError:
        return False


def _month_week_from_title(title: str) -> str:
    """Extract `M#/Wk#` / `M#/Wk#a` style token from tracker title for sheet Month/Week column."""
    t = title.strip()
    # Split articles: M2/Wk5a, M2/Wk5b; pillars: M1/Wk1, M2/Wk10
    m = re.search(r"(?i)\b(M\d+\s*/\s*Wk\d+[a-zA-Z]*)", t)
    if m:
        return m.group(1).replace(" ", "")
    return ""


# Canonical programme labels for sheet Column B (Month/Week), keyed by tracker `content_id`.
# Keeps Month/Week aligned with ID when tracker `title` holds a programme code (M1/Wk1) or a long headline.
_MONTH_WEEK_BY_CONTENT_ID: dict[str, str] = {
    "W01": "M1/Wk1",
    "W02": "M1/Wk2",
    "W03": "M1/Wk3",
    "W04": "M1/Wk4",
    "W05A": "M2/Wk5a",
    "W05B": "M2/Wk5b",
    "W06A": "M2/Wk6a",
    "W06B": "M2/Wk6b",
    "W07A": "M2/Wk7a",
    "W07B": "M2/Wk7b",
    "W09A": "M3/Wk9a",
    "W09B": "M3/Wk9b",
    "W10": "M3/Wk10",
}


def _month_week_for_sheet(content_id: str, tracker_title: str) -> str:
    key = content_id.strip().upper()
    if key in _MONTH_WEEK_BY_CONTENT_ID:
        return _MONTH_WEEK_BY_CONTENT_ID[key]
    return _month_week_from_title(tracker_title)


def _sheet_display_title(tracker_title: str, fm: dict[str, str] | None) -> str:
    """Sheet Title column: prefer `article_title` from 05_Final front matter when present."""
    if fm:
        at = _strip_scalar(str(fm.get("article_title", "") or ""))
        if at:
            return at
    return tracker_title.strip()


def _publish_date_for_sheet(content_id: str, fm: dict[str, str] | None) -> str:
    """
    Publish Date column from `05_Final.md` front matter.

    When one artifact folder backs split tracker rows (e.g. W05A/W05B → `input/W05/`),
    optional keys `publish_date_w05a`, `publish_date_w05b`, … override the generic
    `publish_date` for that `content_id`.
    """
    if not fm:
        return ""
    cid = content_id.strip()
    per_id = _strip_scalar(str(fm.get(f"publish_date_{cid.lower()}", "") or ""))
    if per_id:
        return per_id
    return _strip_scalar(str(fm.get("publish_date", "") or ""))


def _word_count_body(body: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", body))


def _load_final_front_matter(repo_root: Path, wid: str) -> tuple[dict[str, str] | None, str]:
    rel = f"input/{wid.upper()}/05_Final.md"
    path = repo_root / rel
    if not path.is_file():
        return None, ""
    text = path.read_text(encoding="utf-8")
    fm, body = _split_front_matter(text)
    return fm, body


def validate_tracker_columns(df: pd.DataFrame) -> None:
    missing = [c for c in TRACKER_REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"tracker.csv missing required columns: {missing}. "
            f"Expected: {list(TRACKER_REQUIRED_COLUMNS)}"
        )


def authoritative_column_indices() -> list[int]:
    auth = frozenset(AUTHORITATIVE_SYNC_HEADERS)
    return [i for i, h in enumerate(SHEET_HEADERS) if h in auth]


def normalize_header_cell(s: str) -> str:
    return str(s).replace("\ufeff", "").strip()


def validate_sheet_headers_match(found: list[str]) -> tuple[bool, list[str]]:
    errs: list[str] = []
    exp = list(SHEET_HEADERS)
    if len(found) < len(exp):
        errs.append(
            f"Sheet header row has {len(found)} columns; expected {len(exp)} (A–V)."
        )
    for i, eh in enumerate(exp):
        if i >= len(found):
            errs.append(f"Missing column {i + 1}: expected {eh!r}")
            continue
        got = normalize_header_cell(found[i])
        if got != eh:
            errs.append(f"Column {col_index_to_letters(i)}: expected {eh!r}, got {got!r}")
    return len(errs) == 0, errs


def compose_all_tracker_rows(
    tracker_path: Path, repo_root: Path
) -> list[dict[str, str]]:
    df = pd.read_csv(tracker_path)
    validate_tracker_columns(df)
    rows_out: list[dict[str, str]] = []
    for _, series in df.iterrows():
        rows_out.append(compose_sheet_row(series.to_dict(), repo_root))
    rows_out.sort(key=lambda r: _week_id_sort_key(r["ID"]))
    return rows_out


def compose_sheet_row(tracker_row: dict, repo_root: Path) -> dict[str, str]:
    """Build one flat row keyed by SHEET_HEADERS."""
    wid = _tracker_cell(tracker_row["content_id"]).upper()
    bundle = _bundle_wid_for_paths(tracker_row, wid)
    title = _tracker_cell(tracker_row.get("title"))
    status = _tracker_cell(tracker_row.get("status"))
    cur = _tracker_cell(tracker_row.get("current_step"))
    nxt = _tracker_cell(tracker_row.get("next_step"))
    if cur and nxt:
        step = f"{cur} → {nxt}"
    elif cur:
        step = cur
    elif nxt:
        step = nxt
    else:
        step = ""
    status_display = " | ".join(x for x in (status, step) if x)

    fm, body = _load_final_front_matter(repo_root, bundle)
    publish_date = ""
    target_kw = ""
    word_count = ""
    cta = ""
    published_url = ""

    if fm:
        publish_date = _publish_date_for_sheet(wid, fm)
        target_kw = _strip_scalar(str(fm.get("primary_keyword", "") or ""))
        wct = fm.get("word_count_target")
        if wct is not None and str(wct).strip():
            word_count = _strip_scalar(str(wct))
        elif body.strip():
            word_count = str(_word_count_body(body))
        cta = _strip_scalar(str(fm.get("cta_type", "") or ""))
        published_url = _strip_scalar(str(fm.get("canonical_url", "") or ""))

    brief = _exists(repo_root, f"input/{bundle}/01_Content_Brief.md")
    seo_plan = _exists(repo_root, f"input/{bundle}/02_SEO_Plan.md")
    research = _exists(repo_root, f"input/{bundle}/03_Research.md")
    draft = _exists(repo_root, f"input/{bundle}/04_Draft.md")
    final_exists = _exists(repo_root, f"input/{bundle}/05_Final.md")
    assets = all(
        _exists(repo_root, f"input/{bundle}/{name}")
        for name in (
            "06_Design_Brief.md",
            "07_Social_Posts.md",
            "08_Email_Copy.md",
            "09_Publish_Checklist.md",
        )
    )
    email_ok = _nontrivial(repo_root, f"input/{bundle}/08_Email_Copy.md")
    social_ok = _nontrivial(repo_root, f"input/{bundle}/07_Social_Posts.md")

    def yn(x: bool) -> str:
        return "Y" if x else "N"

    if fm and str(fm.get("seo_status", "")).strip():
        seo_cell = _strip_scalar(str(fm.get("seo_status", "")))
    elif final_exists:
        seo_cell = "Y"
    else:
        seo_cell = "N"

    notes = ""
    try:
        cfg = load_canonical_runtime_config()
        if cfg.get("active_week") == wid:
            notes = "active_week"
    except OSError:
        pass

    display_title = _sheet_display_title(title, fm)

    return {
        "ID": wid,
        "Month/Week": _month_week_for_sheet(wid, title),
        "Publish Date": publish_date,
        "Title": display_title,
        "Target Keyword": target_kw,
        "Word Count": word_count,
        "Brief Done": yn(brief),
        "SEO Plan": yn(seo_plan),
        "Research Done": yn(research),
        "Draft Done": yn(draft),
        "Edited": yn(final_exists),
        "SEO Final": seo_cell,
        "Assets Done": yn(assets),
        "Email Ready": yn(email_ok),
        "Social Ready": yn(social_ok),
        "CTA": cta,
        "Published URL": published_url,
        "Status": status_display,
        "Day7 Impr.": "",
        "Day30 Impr.": "",
        "Day30 Clicks": "",
        "Notes": notes,
    }


def dry_run_local_print(
    tracker_path: Path,
    repo_root: Path,
    *,
    out_csv: bool = True,
) -> int:
    if not tracker_path.is_file():
        print(f"ERROR: tracker not found: {tracker_path}", file=sys.stderr)
        return 1

    try:
        rows_out = compose_all_tracker_rows(tracker_path, repo_root)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if out_csv:
        w = csv.writer(sys.stdout, lineterminator="\n")
        w.writerow(list(SHEET_HEADERS))
        for row in rows_out:
            w.writerow([row[h] for h in SHEET_HEADERS])

    print(
        f"\n# Local dry-run OK: {len(rows_out)} row(s). "
        "No Google API calls. Analytics columns left blank.",
        file=sys.stderr,
    )
    return 0


def _audit_path(repo_root: Path, mode: str) -> Path:
    audit_dir = repo_root / "output" / "sheet_sync_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return audit_dir / f"audit_{mode}_{ts}.json"


def _write_audit(repo_root: Path, mode: str, payload: dict) -> Path:
    path = _audit_path(repo_root, mode)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _row_snapshot_for_audit(
    headers: tuple[str, ...], cells: list[str]
) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, h in enumerate(headers):
        if i < len(cells):
            out[h] = cells[i]
        else:
            out[h] = ""
    return out


def _diff_authoritative_columns(
    before: dict[str, str], after: dict[str, str]
) -> list[str]:
    changed: list[str] = []
    for k in AUTHORITATIVE_SYNC_HEADERS:
        if str(before.get(k, "")).strip() != str(after.get(k, "")).strip():
            changed.append(k)
    return changed


def _governance_prefix_warnings(
    inserted_ids: list[str], sheet_ids_before: set[str]
) -> list[str]:
    """Warn when a new insert shares a prefix with existing sheet IDs (e.g. W05 vs W05A)."""
    notes: list[str] = []
    for iid in inserted_ids:
        overlaps = sorted(
            s for s in sheet_ids_before if s != iid and s.startswith(iid)
        )
        if overlaps:
            notes.append(
                f"Governance: inserted tracker id {iid!r}; sheet already had distinct ids "
                f"{overlaps} (exact match only — no merge). Confirm intentional."
            )
    return notes


def run_sheet_dry_run_google(tracker_path: Path, repo_root: Path) -> int:
    """Phase 4B.1 — validate tracker + sheet headers/tab + planned upserts; no writes."""
    if not tracker_path.is_file():
        print(f"ERROR: tracker not found: {tracker_path}", file=sys.stderr)
        return 1

    try:
        env = load_sheet_env(require_tab=True)
        composed = compose_all_tracker_rows(tracker_path, repo_root)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    num_cols = len(SHEET_HEADERS)
    try:
        service = build_sheets_service(env.sa_path, readonly=True)
        sheet_headers = fetch_sheet_headers(service, env, num_cols=num_cols)
        ok, hdr_errs = validate_sheet_headers_match(sheet_headers)
        id_to_row = fetch_id_to_row(service, env)
    except Exception as e:
        print(f"ERROR: Google Sheets API failed: {e}", file=sys.stderr)
        try:
            hint = load_sa_email(env.sa_path)
        except Exception:
            hint = "(set WORKCREW_GOOGLE_SERVICE_ACCOUNT)"
        print(
            f"Hint: Share the spreadsheet with the service account (Editor):\n  {hint}",
            file=sys.stderr,
        )
        return 1

    tracker_ids = [r[SYNC_ROW_KEY_HEADER] for r in composed]
    tracker_set = set(tracker_ids)
    sheet_ids = set(id_to_row.keys())

    planned_updates: list[dict] = []
    planned_inserts: list[str] = []
    skipped_preview: list[dict] = []

    auth_idx = authoritative_column_indices()

    for row in composed:
        wid = row[SYNC_ROW_KEY_HEADER]
        if wid not in id_to_row:
            planned_inserts.append(wid)
            continue
        row_num = id_to_row[wid]
        try:
            cur_unfmt = fetch_row_display(
                service,
                env,
                row_num,
                num_cols=num_cols,
                value_render_option="UNFORMATTED_VALUE",
            )
            formulas = fetch_row_display(
                service,
                env,
                row_num,
                num_cols=num_cols,
                value_render_option="FORMULA",
            )
        except Exception as e:
            skipped_preview.append({"id": wid, "reason": f"read_failed: {e}"})
            continue

        would_write_cols: list[str] = []
        for ci in auth_idx:
            hname = SHEET_HEADERS[ci]
            if formulas[ci].strip().startswith("="):
                skipped_preview.append(
                    {"id": wid, "column": hname, "reason": "formula_cell_skip"}
                )
                continue
            new_v = row[hname]
            old_v = cur_unfmt[ci] if ci < len(cur_unfmt) else ""
            if str(old_v).strip() != str(new_v).strip():
                would_write_cols.append(hname)

        if would_write_cols:
            planned_updates.append({"id": wid, "sheet_row": row_num, "columns": would_write_cols})

    orphans = sorted(sheet_ids - tracker_set)

    iso_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "timestamp_utc": iso_now,
        "mode": "dry_run",
        "row_key_header": SYNC_ROW_KEY_HEADER,
        "tracker_source": str(tracker_path.resolve()),
        "tracker_row_count": len(composed),
        "sheet_tab": env.tab_title,
        "sheet_header_row_1based": env.header_row_1based,
        "expected_headers": list(SHEET_HEADERS),
        "sheet_headers_observed": sheet_headers[:num_cols],
        "headers_match": ok,
        "header_validation_errors": hdr_errs,
        "sheet_distinct_id_rows": len(id_to_row),
        "planned_inserts": planned_inserts,
        "planned_updates": planned_updates,
        "sheet_row_ids_not_in_tracker": orphans,
        "skipped_or_notes": skipped_preview,
        "authoritative_columns": list(AUTHORITATIVE_SYNC_HEADERS),
        "analytics_columns_excluded_from_write": sorted(SHEET_ANALYTICS_COLUMNS),
    }

    audit_file = _write_audit(repo_root, "dry_run", payload)

    print("Phase 4B.1 dry-run (no sheet writes)", file=sys.stderr)
    print(f"Tracker rows: {len(composed)}", file=sys.stderr)
    print(f"Sheet IDs found (column A): {len(id_to_row)}", file=sys.stderr)
    print(f"Headers match contract: {ok}", file=sys.stderr)
    if not ok:
        for line in hdr_errs:
            print(f"  HEADER: {line}", file=sys.stderr)
        print(f"Audit: {audit_file}", file=sys.stderr)
        return 1
    print(f"Planned inserts: {len(planned_inserts)} {planned_inserts}", file=sys.stderr)
    print(f"Planned updates: {len(planned_updates)}", file=sys.stderr)
    print(f"Sheet-only IDs (not in tracker, left untouched): {orphans}", file=sys.stderr)
    print(f"Audit written: {audit_file}", file=sys.stderr)
    return 0


def run_sheet_write(tracker_path: Path, repo_root: Path) -> int:
    """Phase 4B.2/4B.3 — upsert authoritative columns; audit with before/after snapshots."""
    if not tracker_path.is_file():
        print(f"ERROR: tracker not found: {tracker_path}", file=sys.stderr)
        return 1

    try:
        env = load_sheet_env(require_tab=True)
        composed = compose_all_tracker_rows(tracker_path, repo_root)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    num_cols = len(SHEET_HEADERS)
    auth_idx = authoritative_column_indices()
    tab_a1 = a1_escape_tab(env.tab_title)

    try:
        service = build_sheets_service(env.sa_path, readonly=False)
        sheet_headers = fetch_sheet_headers(service, env, num_cols=num_cols)
        ok, hdr_errs = validate_sheet_headers_match(sheet_headers)
        if not ok:
            for line in hdr_errs:
                print(f"ERROR: HEADER {line}", file=sys.stderr)
            return 1
        id_to_row = fetch_id_to_row(service, env)
    except Exception as e:
        print(f"ERROR: Google Sheets API failed: {e}", file=sys.stderr)
        try:
            hint = load_sa_email(env.sa_path)
        except Exception:
            hint = "(set WORKCREW_GOOGLE_SERVICE_ACCOUNT)"
        print(
            f"Hint: Share the spreadsheet with the service account (Editor):\n  {hint}",
            file=sys.stderr,
        )
        return 1

    tracker_set = {r[SYNC_ROW_KEY_HEADER] for r in composed}

    changed_rows: list[dict] = []
    failed_rows: list[dict] = []
    skipped_cells_formula: list[dict] = []
    skipped_rows: list[dict] = []

    sheet_ids_before_writes = set(id_to_row.keys())
    append_cursor = next_blank_row_after_ids(service, env)

    for row in composed:
        wid = row[SYNC_ROW_KEY_HEADER]
        try:
            if wid in id_to_row:
                row_num = id_to_row[wid]
                before_cells = fetch_row_display(
                    service,
                    env,
                    row_num,
                    num_cols=num_cols,
                    value_render_option="UNFORMATTED_VALUE",
                )
                formulas = fetch_row_display(
                    service,
                    env,
                    row_num,
                    num_cols=num_cols,
                    value_render_option="FORMULA",
                )
                before_snap = _row_snapshot_for_audit(SHEET_HEADERS, before_cells)

                batch_ranges: list[tuple[str, list[list[str]]]] = []
                for ci in auth_idx:
                    hname = SHEET_HEADERS[ci]
                    letter = col_index_to_letters(ci)
                    if formulas[ci].strip().startswith("="):
                        skipped_cells_formula.append(
                            {"id": wid, "row": row_num, "column": hname}
                        )
                        continue
                    new_v = row[hname]
                    old_v = before_cells[ci] if ci < len(before_cells) else ""
                    if str(old_v).strip() == str(new_v).strip():
                        continue
                    rng = f"{tab_a1}!{letter}{row_num}"
                    batch_ranges.append((rng, [[new_v]]))

                if batch_ranges:
                    batch_update_cells(service, env.spreadsheet_id, batch_ranges)
                    after_cells = fetch_row_display(
                        service,
                        env,
                        row_num,
                        num_cols=num_cols,
                        value_render_option="UNFORMATTED_VALUE",
                    )
                    after_snap = _row_snapshot_for_audit(SHEET_HEADERS, after_cells)
                    before_auth = {
                        k: before_snap[k] for k in AUTHORITATIVE_SYNC_HEADERS
                    }
                    after_auth = {
                        k: after_snap[k] for k in AUTHORITATIVE_SYNC_HEADERS
                    }
                    cols_changed = _diff_authoritative_columns(before_auth, after_auth)
                    changed_rows.append(
                        {
                            "id": wid,
                            "sheet_row": row_num,
                            "operation": "update",
                            "columns_changed": cols_changed,
                            "before_authoritative": before_auth,
                            "after_authoritative": after_auth,
                        }
                    )
                    delay = _sheet_sync_row_delay_s()
                    if delay:
                        time.sleep(delay)
            else:
                before_snap = {h: "" for h in SHEET_HEADERS}
                batch_ranges = []
                for ci in auth_idx:
                    hname = SHEET_HEADERS[ci]
                    letter = col_index_to_letters(ci)
                    rng = f"{tab_a1}!{letter}{append_cursor}"
                    batch_ranges.append((rng, [[row[hname]]]))
                if batch_ranges:
                    batch_update_cells(service, env.spreadsheet_id, batch_ranges)
                    after_cells = fetch_row_display(
                        service,
                        env,
                        append_cursor,
                        num_cols=num_cols,
                        value_render_option="UNFORMATTED_VALUE",
                    )
                    after_snap = _row_snapshot_for_audit(SHEET_HEADERS, after_cells)
                    before_auth = {
                        k: before_snap[k] for k in AUTHORITATIVE_SYNC_HEADERS
                    }
                    after_auth = {
                        k: after_snap[k] for k in AUTHORITATIVE_SYNC_HEADERS
                    }
                    cols_changed = _diff_authoritative_columns(before_auth, after_auth)
                    changed_rows.append(
                        {
                            "id": wid,
                            "sheet_row": append_cursor,
                            "operation": "insert",
                            "columns_changed": cols_changed,
                            "before_authoritative": before_auth,
                            "after_authoritative": after_auth,
                        }
                    )
                    id_to_row[wid] = append_cursor
                    append_cursor += 1
                    delay = _sheet_sync_row_delay_s()
                    if delay:
                        time.sleep(delay)
        except Exception as e:
            failed_rows.append({"id": wid, "error": str(e)})

    reorder_log: list[str] = []
    if not failed_rows and composed:
        try:
            reorder_log = reorder_data_rows_to_composed_order(service, env, composed)
        except Exception as e:
            print(f"ERROR: Row reorder failed: {e}", file=sys.stderr)
            return 1
        id_to_row = fetch_id_to_row(service, env)

    iso_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inserted_entries = [r for r in changed_rows if r.get("operation") == "insert"]
    updated_entries = [r for r in changed_rows if r.get("operation") == "update"]
    governance_warnings = _governance_prefix_warnings(
        [r["id"] for r in inserted_entries],
        sheet_ids_before_writes,
    )

    payload = {
        "audit_timestamp": iso_now,
        "timestamp_utc": iso_now,
        "mode": "write",
        "row_key_header": SYNC_ROW_KEY_HEADER,
        "tracker_source": str(tracker_path.resolve()),
        "tracker_row_count": len(composed),
        "sheet_tab": env.tab_title,
        "headers_match_contract": True,
        "inserted_rows": len(inserted_entries),
        "inserted_row_ids": [r["id"] for r in inserted_entries],
        "updated_rows": len(updated_entries),
        "updated_row_ids": [r["id"] for r in updated_entries],
        "skipped_formula_cells_count": len(skipped_cells_formula),
        "skipped_formula_cells": skipped_cells_formula,
        "skipped_cells_formula": skipped_cells_formula,
        "failed_rows": failed_rows,
        "skipped_rows": skipped_rows,
        "governance": {
            "row_key": SYNC_ROW_KEY_HEADER,
            "match_rule": (
                "Exact match only: sheet column A must equal tracker content_id. "
                "No fuzzy merge; distinct sheet IDs (e.g. W05A) are never overwritten by W05."
            ),
            "warnings": governance_warnings,
        },
        "row_reorder_log": reorder_log,
        "changed_rows": changed_rows,
        "sheet_row_ids_not_in_tracker": sorted(set(id_to_row.keys()) - tracker_set),
        "authoritative_columns_written": list(AUTHORITATIVE_SYNC_HEADERS),
        "analytics_columns_never_written": sorted(SHEET_ANALYTICS_COLUMNS),
    }
    audit_file = _write_audit(repo_root, "write", payload)

    print("Phase 4B.2 write complete", file=sys.stderr)
    print("Headers match contract: True", file=sys.stderr)
    print(f"Inserted rows: {len(inserted_entries)} { [r['id'] for r in inserted_entries] }", file=sys.stderr)
    print(f"Updated rows: {len(updated_entries)} { [r['id'] for r in updated_entries] }", file=sys.stderr)
    print(f"Rows with cell changes (insert+update): {len(changed_rows)}", file=sys.stderr)
    print(f"Failed rows: {len(failed_rows)}", file=sys.stderr)
    print(f"Skipped formula cells: {len(skipped_cells_formula)}", file=sys.stderr)
    for gw in governance_warnings:
        print(f"WARNING: {gw}", file=sys.stderr)
    if reorder_log:
        print("Row reorder (chronological week order):", file=sys.stderr)
        for line in reorder_log:
            print(f"  {line}", file=sys.stderr)
    print(f"Audit written: {audit_file}", file=sys.stderr)
    print(
        "\nValidations: (1) review lines above for errors; "
        f"(2) open the Google Sheet tab {env.tab_title!r} in the browser and confirm rows look right; "
        "(3) open the audit JSON for inserted_row_ids, updated_row_ids, columns_changed per row. "
        "If you use a separate reader tab, refresh formulas or paste from this mirror tab as needed.",
        file=sys.stderr,
    )
    return 0 if not failed_rows else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Sheet mirror: tracker + repo → 22-column stakeholder layout. "
            "Default: local CSV only. Use --dry-run for 4B.1 (sheet validation + audit). "
            "Use --write for 4B.2 controlled upsert."
        )
    )
    parser.add_argument(
        "--tracker",
        type=Path,
        default=TRACKER_PATH,
        help=f"Path to tracker.csv (default: {TRACKER_PATH})",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root for input/WXX/ paths",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Phase 4B.1: validate tracker + Google Sheet tab/headers + planned changes; no writes.",
    )
    parser.add_argument(
        "-w",
        "--write",
        action="store_true",
        help="Phase 4B.2: upsert authoritative columns only (no analytics overwrite); audit on disk.",
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="With --dry-run: skip Google APIs (local validation + CSV stdout only).",
    )
    args = parser.parse_args()
    tracker = args.tracker.resolve()
    root = args.repo_root.resolve()

    if args.write and args.dry_run:
        print("ERROR: Use either --dry-run or --write, not both.", file=sys.stderr)
        sys.exit(2)

    if args.write:
        sys.exit(run_sheet_write(tracker, root))

    if args.dry_run:
        if args.local_only:
            sys.exit(dry_run_local_print(tracker, root))
        sys.exit(run_sheet_dry_run_google(tracker, root))

    sys.exit(dry_run_local_print(tracker, root))


if __name__ == "__main__":
    main()
