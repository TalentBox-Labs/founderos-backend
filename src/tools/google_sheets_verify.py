"""
Read-only Google Sheets API verification (Phase 4A — credentials check).

Confirms: authenticate → open spreadsheet → read metadata (+ optional tab title).
Does NOT write to the sheet.

Requires env:
  WORKCREW_GOOGLE_SERVICE_ACCOUNT — absolute path to service account JSON
  WORKCREW_GOOGLE_SHEET_ID — spreadsheet id from the URL

Optional:
  WORKCREW_GOOGLE_SHEET_TAB — if set, checks that a sheet with this title exists

Loads `.env` from repo root when python-dotenv is installed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from src.tools.runtime_paths import REPO_ROOT
from src.tools.env_compat import getenv_compat

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass


def _load_sa_email(sa_path: Path) -> str:
    data = json.loads(sa_path.read_text(encoding="utf-8"))
    email = data.get("client_email")
    if not email:
        raise ValueError("JSON missing client_email")
    return str(email)


def verify() -> int:
    sa_raw = getenv_compat("GOOGLE_SERVICE_ACCOUNT", "").strip()
    sheet_id = getenv_compat("GOOGLE_SHEET_ID", "").strip()
    tab_want = getenv_compat("GOOGLE_SHEET_TAB", "").strip()

    if not sa_raw:
        print(
            "ERROR: Set WORKCREW_GOOGLE_SERVICE_ACCOUNT to the absolute path "
            "of your service account JSON.",
            file=sys.stderr,
        )
        return 2
    if not sheet_id:
        print("ERROR: Set WORKCREW_GOOGLE_SHEET_ID.", file=sys.stderr)
        return 2

    sa_path = Path(sa_raw).expanduser()
    if not sa_path.is_file():
        print(f"ERROR: Service account file not found: {sa_path}", file=sys.stderr)
        return 2

    try:
        client_email = _load_sa_email(sa_path)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: Invalid service account JSON: {e}", file=sys.stderr)
        return 2

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        print(
            "ERROR: Install Google client libraries:\n"
            "  pip install google-api-python-client google-auth google-auth-httplib2",
            file=sys.stderr,
        )
        return 2

    scopes = ("https://www.googleapis.com/auth/spreadsheets.readonly",)
    creds = service_account.Credentials.from_service_account_file(
        str(sa_path), scopes=scopes
    )
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    try:
        meta = (
            service.spreadsheets()
            .get(
                spreadsheetId=sheet_id,
                fields="properties.title,sheets(properties(title,sheetId))",
            )
            .execute()
        )
    except Exception as e:
        print(f"ERROR: API request failed: {e}", file=sys.stderr)
        print(
            f"Hint: Share the spreadsheet with this service account as Editor:\n"
            f"  {client_email}",
            file=sys.stderr,
        )
        return 1

    title = (meta.get("properties") or {}).get("title", "")
    sheets = meta.get("sheets") or []
    tab_titles = [
        (s.get("properties") or {}).get("title", "") for s in sheets
    ]

    print("Can authenticate")
    print("Can open sheet")
    print(f"Spreadsheet title: {title!r}")
    print(f"Sheet tabs ({len(tab_titles)}): {', '.join(tab_titles)}")
    print("Can read sheet metadata")

    if tab_want:
        if tab_want in tab_titles:
            print(f"Tab {tab_want!r}: OK")
        else:
            print(
                f"WARNING: WORKCREW_GOOGLE_SHEET_TAB={tab_want!r} not found. "
                f"Available: {tab_titles}",
                file=sys.stderr,
            )
            return 1

    print("Not writing yet.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify Google Sheets API read access (no writes)."
    )
    parser.parse_args()
    sys.exit(verify())


if __name__ == "__main__":
    main()
