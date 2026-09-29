"""CMS go-live helpers: print the publish checklist.

``record-live`` does not stamp ``published``. Browser confirmation is not a
remote CMS receipt. Existing files stay as they are.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import yaml

from src.tools.runtime_paths import REPO_ROOT, TRACKER_PATH


def _read_tracker_rows() -> tuple[list[str], list[dict[str, str]]]:
    with TRACKER_PATH.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    return fieldnames, rows


def _final_path_for_row(row: dict[str, str]) -> Path:
    final_rel = (row.get("final_output_path") or "").strip()
    if final_rel:
        return REPO_ROOT / final_rel
    draft = (row.get("draft_path") or "").strip()
    if draft:
        return REPO_ROOT / Path(draft).parent / "05_Final.md"
    raise ValueError(f"No final path for content_id={row.get('content_id')!r}")


def _parse_front_matter(raw: str) -> tuple[dict, str]:
    if not raw.lstrip().startswith("---"):
        return {}, raw
    lines = raw.splitlines()
    if len(lines) < 2 or lines[0].strip() != "---":
        return {}, raw
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, raw
    block = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    try:
        fm = yaml.safe_load(block) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML front matter: {e}") from e
    if not isinstance(fm, dict):
        return {}, raw
    return fm, body


def _compose_final(fm: dict, body: str) -> str:
    dump = yaml.safe_dump(
        fm,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    ).rstrip()
    if body and not body.startswith("\n"):
        body = "\n" + body
    return f"---\n{dump}\n---{body}"


def print_checklist() -> int:
    _, rows = _read_tracker_rows()
    lines: list[str] = []
    lines.append("# CMS go-live checklist (from tracker + 05_Final.md)\n")
    lines.append("| content_id | title | canonical_url | publish_status (file) |\n")
    lines.append("|------------|-------|---------------|-------------------------|\n")
    count = 0
    for row in rows:
        nxt = (row.get("next_step") or "").strip()
        if nxt != "CMS Go-live":
            continue
        cid = (row.get("content_id") or "").strip()
        title = (row.get("title") or "").strip()
        fp = _final_path_for_row(row)
        if not fp.is_file():
            lines.append(f"| {cid} | {title} | **missing file** `{fp.relative_to(REPO_ROOT)}` | — |\n")
            count += 1
            continue
        text = fp.read_text(encoding="utf-8")
        fm, _ = _parse_front_matter(text)
        url = str(fm.get("canonical_url", "")).strip()
        ps = str(fm.get("publish_status", "")).strip()
        lines.append(f"| {cid} | {title} | {url} | {ps} |\n")
        count += 1
    if count == 0:
        lines.append("| — | *(no rows with next_step = CMS Go-live)* | — | — |\n")
    report = "".join(lines)
    print(report, end="")
    return 0


def record_live(content_ids: list[str], *, confirmed: bool) -> int:
    """Refuse to stamp published from a browser confirmation.

    Existing finals and tracker rows are left unchanged. A human looking at a
    URL is not a CMS write receipt.
    """
    if not confirmed:
        print(
            "Refusing to record live state without --i-confirmed-url-live "
            "(confirm in browser the canonical_url is live on workcrew.ai).",
            file=sys.stderr,
        )
        return 2
    if not content_ids:
        print("No content_id values given.", file=sys.stderr)
        return 2
    print(
        "Refusing to mark content published. Browser confirmation is not "
        "remote write proof. Existing files were left unchanged.",
        file=sys.stderr,
    )
    return 3


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("print-checklist", help="Markdown table for rows awaiting CMS go-live")

    rp = sub.add_parser(
        "record-live",
        help="Refuse to stamp published without a remote write receipt",
    )
    rp.add_argument("content_ids", nargs="+", help="e.g. W01 W03")
    rp.add_argument(
        "--i-confirmed-url-live",
        action="store_true",
        required=True,
        help="Required safeguard: you verified canonical_url in a browser.",
    )

    args = p.parse_args(argv)
    if args.cmd == "print-checklist":
        return print_checklist()
    if args.cmd == "record-live":
        return record_live(args.content_ids, confirmed=args.i_confirmed_url_live)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
