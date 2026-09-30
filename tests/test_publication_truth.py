"""Adversarial publication-truth tests. No synthetic CMS receipt is stored."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.tools import editorial_approval as ea
from src.tools import publication_truth as truth
from src.tools import publishing_engine as pe
from src.tools.website_engine.provider import WebsitePublicationRequest
from src.tools.website_engine.publish_result import success_result
from src.tools.website_engine.static_provider import StaticWebsiteProvider
from tests.test_static_provider import _request


@pytest.fixture
def publishing_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    decisions = tmp_path / "output" / "editorial_decisions"
    publishing = tmp_path / "output" / "publishing"
    publishing.mkdir(parents=True)
    decisions.mkdir(parents=True)
    record = {
        "schema_version": 1,
        "decision_id": "dec-test-1",
        "utc_timestamp": "2026-08-09T12:00:00Z",
        "content_id": "W99",
        "phase": "2b",
        "bundle": "output/generated/W99",
        "decision": "approve",
        "approver": "Editor Human",
        "notes": "ok",
        "promotion": {"attempted": True, "ok": True, "exit_code": 0},
        "authorizes_publish": False,
    }
    (decisions / "decisions.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(ea, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ea, "DECISIONS_DIR", decisions)
    monkeypatch.setattr(ea, "HISTORY_JSONL", decisions / "decisions.jsonl")
    monkeypatch.setattr(pe, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(pe, "PUBLISHING_DIR", publishing)
    monkeypatch.setattr(pe, "JOBS_JSONL", publishing / "jobs.jsonl")
    monkeypatch.setattr(pe, "AUDIT_JSONL", publishing / "audit.jsonl")
    monkeypatch.setattr(pe, "JOBS_DIR", publishing / "jobs")
    return {"tmp_path": tmp_path}


def test_ok_without_rendering_is_not_published(publishing_env: dict[str, Any]) -> None:
    job = pe.create_publish_job(
        content_id="W99",
        channel="website",
        requested_by="Human A",
        tenant_id="tenant-a",
    )
    result = pe.manual_publish(job["job_id"], requested_by="Human A")
    assert result["adapter_result"]["ok"] is True
    assert result["adapter_result"]["rendering_performed"] is False
    assert result["state"] != pe.STATE_PUBLISHED
    assert result["publication_truth"] == truth.TRUTH_UNPROVEN


def test_local_static_write_is_not_published(
    publishing_env: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    written = StaticWebsiteProvider(output_dir=tmp_path / "site").publish(request)
    assert written.ok is True
    assert written.external_http is False
    assert any((tmp_path / "site").rglob("index.html"))

    def _static_shaped(_job: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "http_status": 200,
            "rendering_performed": True,
            "external_http": False,
            "artifact_paths": written.artifact_paths,
            "message": written.message,
        }

    monkeypatch.setitem(pe.ADAPTERS, "website", _static_shaped)
    job = pe.create_publish_job(
        content_id="W99",
        channel="website",
        requested_by="Human A",
        tenant_id="tenant-a",
    )
    result = pe.manual_publish(job["job_id"], requested_by="Human A")
    assert result["state"] != pe.STATE_PUBLISHED
    assert result["publication_truth"] == truth.TRUTH_UNPROVEN


def test_http_success_without_remote_write_is_not_published(
    publishing_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _http_only(_job: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "http_status": 200,
            "rendering_performed": True,
            "external_http": True,
        }

    monkeypatch.setitem(pe.ADAPTERS, "website", _http_only)
    job = pe.create_publish_job(
        content_id="W99",
        channel="website",
        requested_by="Human A",
        tenant_id="tenant-a",
    )
    result = pe.manual_publish(job["job_id"], requested_by="Human A")
    assert result["state"] != pe.STATE_PUBLISHED
    assert result["publication_truth"] == truth.TRUTH_UNPROVEN


def test_missing_remote_id_cannot_fabricate_published() -> None:
    classified = truth.classify_adapter_result(
        {
            "ok": True,
            "http_status": 201,
            "remote_write_acknowledged": True,
            "external_http": True,
            "rendering_performed": True,
            "remote_object_id": "   ",
        }
    )
    assert classified == truth.TRUTH_UNPROVEN


def test_frontmatter_and_tracker_cannot_prove_publication() -> None:
    assert truth.local_claim_publication_truth("published") == truth.TRUTH_UNPROVEN
    assert truth.local_claim_publication_truth("QA Passed") == truth.TRUTH_UNPROVEN
    assert (
        truth.verification_from_readback({"publish_status": "published"})
        == truth.TRUTH_VERIFICATION_PENDING
    )


def test_ambiguous_remote_result_does_not_write_again(
    publishing_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def _ambiguous(_job: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        return {
            "ok": True,
            "remote_request_dispatched": True,
            "remote_outcome": "timeout",
            "http_status": 200,
        }

    monkeypatch.setitem(pe.ADAPTERS, "website", _ambiguous)
    job = pe.create_publish_job(
        content_id="W99",
        channel="website",
        requested_by="Human A",
        tenant_id="tenant-a",
    )
    result = pe.manual_publish(job["job_id"], requested_by="Human A")
    assert result["publication_truth"] == truth.TRUTH_UNKNOWN_REMOTE
    assert result["remote_write_outstanding"] is True
    with pytest.raises(ValueError, match="unknown"):
        pe.retry_job(job["job_id"], requested_by="Human A")
    with pytest.raises(ValueError, match="unknown"):
        pe.manual_publish(job["job_id"], requested_by="Human A")
    assert calls["n"] == 1


def test_verified_requires_later_readback() -> None:
    assert (
        truth.verification_from_readback(
            {
                "readback_performed": True,
                "remote_object_id": "cms-object-1",
                "content_hash": "abc",
                "readback_matches": True,
            }
        )
        == truth.TRUTH_VERIFIED
    )
    acknowledged = truth.classify_adapter_result(
        {
            "ok": True,
            "remote_write_acknowledged": True,
            "remote_object_id": "cms-object-1",
            "external_http": True,
            "rendering_performed": True,
        }
    )
    assert acknowledged == truth.TRUTH_REMOTE_WRITE_CONFIRMED
    assert acknowledged != truth.TRUTH_VERIFIED


def test_provider_http_body_without_id_is_unproven() -> None:
    status: dict[str, Any] = {}
    got = truth.apply_provider_body_to_channel_status(
        status, "hashnode", {"ok": True, "http_status": 200}
    )
    assert got == truth.TRUTH_UNPROVEN
    assert status.get("published", {}).get("hashnode") is not True


def test_website_success_channel_stays_unproven() -> None:
    channel = success_result(
        message="local",
        content_id="W99",
        slug="w99",
        canonical_url="https://example.test/w99",
        rendering_performed=True,
    ).to_channel_result()
    assert channel["ok"] is True
    assert channel["external_http"] is False
    assert truth.classify_adapter_result(channel) == truth.TRUTH_UNPROVEN


def test_static_request_type_is_local(tmp_path: Path) -> None:
    request: WebsitePublicationRequest = _request()
    result = StaticWebsiteProvider(output_dir=tmp_path).publish(request)
    assert result.external_http is False
