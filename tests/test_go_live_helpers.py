"""Tests for go_live_helpers (isolated tmp tracker + finals)."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

import src.tools.go_live_helpers as gl


TRACKER_HEADER = [
    "content_id",
    "title",
    "status",
    "qa_status",
    "current_step",
    "next_step",
    "draft_path",
    "qa_output_path",
    "final_output_path",
    "artifact_folder",
]


def _write_tracker(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRACKER_HEADER)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in TRACKER_HEADER})


def test_print_checklist_filters_cms_go_live(tmp_path, monkeypatch, capsys):
    root = tmp_path
    inv = root / "input" / "WX" / "05_Final.md"
    inv.parent.mkdir(parents=True)
    inv.write_text(
        "---\n"
        "canonical_url: https://example.test/a\n"
        "publish_status: Ready\n"
        "---\n\n# Body\n",
        encoding="utf-8",
    )
    csv_path = root / "tracker.csv"
    _write_tracker(
        csv_path,
        [
            {
                "content_id": "WX",
                "title": "T",
                "status": "QA Passed",
                "qa_status": "PASS",
                "current_step": "Publish Review",
                "next_step": "CMS Go-live",
                "draft_path": "input/WX/04_Draft.md",
                "qa_output_path": "",
                "final_output_path": "input/WX/05_Final.md",
                "artifact_folder": "",
            },
            {
                "content_id": "WY",
                "title": "Other",
                "status": "QA Passed",
                "qa_status": "PASS",
                "current_step": "Planning",
                "next_step": "Research",
                "draft_path": "input/WY/04_Draft.md",
                "qa_output_path": "",
                "final_output_path": "input/WY/05_Final.md",
                "artifact_folder": "",
            },
        ],
    )
    monkeypatch.setattr(gl, "TRACKER_PATH", csv_path)
    monkeypatch.setattr(gl, "REPO_ROOT", root)

    assert gl.print_checklist() == 0
    out = capsys.readouterr().out
    assert "WX" in out
    assert "https://example.test/a" in out
    assert "WY" not in out


def test_record_live_updates_ready_and_tracker(tmp_path, monkeypatch):
    root = tmp_path
    fin = root / "input" / "WZ" / "05_Final.md"
    fin.parent.mkdir(parents=True)
    fin.write_text(
        "---\n"
        "week_id: WZ\n"
        "article_title: A\n"
        "publish_status: Ready\n"
        "canonical_url: https://example.test/z\n"
        "cta_type: try_workcrew_free\n"
        "---\n\n# Hi\n",
        encoding="utf-8",
    )
    csv_path = root / "tracker.csv"
    _write_tracker(
        csv_path,
        [
            {
                "content_id": "WZ",
                "title": "T",
                "status": "QA Passed",
                "qa_status": "PASS",
                "current_step": "Publish Review",
                "next_step": "CMS Go-live",
                "draft_path": "input/WZ/04_Draft.md",
                "qa_output_path": "",
                "final_output_path": "input/WZ/05_Final.md",
                "artifact_folder": "",
            },
        ],
    )
    monkeypatch.setattr(gl, "TRACKER_PATH", csv_path)
    monkeypatch.setattr(gl, "REPO_ROOT", root)

    assert gl.record_live(["WZ"], confirmed=True) == 3
    text = fin.read_text(encoding="utf-8")
    assert "publish_status: Ready" in text
    assert "publish_status: published" not in text.lower()
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert rows[0]["current_step"] == "Publish Review"
    assert rows[0]["next_step"] == "CMS Go-live"


def test_record_live_idempotent_when_already_published(tmp_path, monkeypatch):
    root = tmp_path
    fin = root / "input" / "WA" / "05_Final.md"
    fin.parent.mkdir(parents=True)
    fin.write_text(
        "---\n"
        "week_id: WA\n"
        "article_title: A\n"
        "publish_status: published\n"
        "canonical_url: https://example.test/a\n"
        "cta_type: try_workcrew_free\n"
        "---\n\n# Hi\n",
        encoding="utf-8",
    )
    csv_path = root / "tracker.csv"
    _write_tracker(
        csv_path,
        [
            {
                "content_id": "WA",
                "title": "T",
                "status": "QA Passed",
                "qa_status": "PASS",
                "current_step": "Publish Review",
                "next_step": "CMS Go-live",
                "draft_path": "input/WA/04_Draft.md",
                "qa_output_path": "",
                "final_output_path": "input/WA/05_Final.md",
                "artifact_folder": "",
            },
        ],
    )
    monkeypatch.setattr(gl, "TRACKER_PATH", csv_path)
    monkeypatch.setattr(gl, "REPO_ROOT", root)

    before = fin.read_text(encoding="utf-8")
    assert gl.record_live(["WA"], confirmed=True) == 3
    assert fin.read_text(encoding="utf-8") == before
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert rows[0]["current_step"] == "Publish Review"
    assert rows[0]["next_step"] == "CMS Go-live"


def test_record_live_rejects_bad_publish_status(tmp_path, monkeypatch):
    root = tmp_path
    fin = root / "input" / "WB" / "05_Final.md"
    fin.parent.mkdir(parents=True)
    fin.write_text(
        "---\n"
        "publish_status: Draft\n"
        "canonical_url: https://example.test/b\n"
        "---\n\n# Hi\n",
        encoding="utf-8",
    )
    csv_path = root / "tracker.csv"
    _write_tracker(
        csv_path,
        [
            {
                "content_id": "WB",
                "title": "T",
                "status": "QA Passed",
                "qa_status": "PASS",
                "current_step": "Publish Review",
                "next_step": "CMS Go-live",
                "draft_path": "input/WB/04_Draft.md",
                "qa_output_path": "",
                "final_output_path": "input/WB/05_Final.md",
                "artifact_folder": "",
            },
        ],
    )
    monkeypatch.setattr(gl, "TRACKER_PATH", csv_path)
    monkeypatch.setattr(gl, "REPO_ROOT", root)

    before = fin.read_text(encoding="utf-8")
    assert gl.record_live(["WB"], confirmed=True) == 3
    assert fin.read_text(encoding="utf-8") == before
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert rows[0]["current_step"] == "Publish Review"
