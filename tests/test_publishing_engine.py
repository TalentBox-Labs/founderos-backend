"""Focused tests for Publishing Engine Phase 1 (Sprint M1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.tools import editorial_approval as ea
from src.tools import publishing_engine as pe


def _create_job(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("tenant_id", "tenant-a")
    return pe.create_publish_job(**kwargs)


def _seed_editorial_approve(
    decisions_dir: Path, content_id: str = "W99", phase: str = "2b"
) -> None:
    decisions_dir.mkdir(parents=True, exist_ok=True)
    hist = decisions_dir / "decisions.jsonl"
    rec = {
        "schema_version": 1,
        "decision_id": "dec-test-1",
        "utc_timestamp": "2026-08-09T12:00:00Z",
        "content_id": content_id,
        "phase": phase,
        "bundle": f"output/generated/{content_id}",
        "decision": "approve",
        "approver": "Editor Human",
        "notes": "ok",
        "promotion": {"attempted": True, "ok": True, "exit_code": 0},
        "authorizes_publish": False,
    }
    with hist.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


@pytest.fixture
def publishing_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> dict[str, Any]:
    decisions = tmp_path / "output" / "editorial_decisions"
    publishing = tmp_path / "output" / "publishing"
    publishing.mkdir(parents=True)
    _seed_editorial_approve(decisions)

    monkeypatch.setattr(ea, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ea, "DECISIONS_DIR", decisions)
    monkeypatch.setattr(ea, "HISTORY_JSONL", decisions / "decisions.jsonl")
    monkeypatch.setattr(pe, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(pe, "PUBLISHING_DIR", publishing)
    monkeypatch.setattr(pe, "JOBS_JSONL", publishing / "jobs.jsonl")
    monkeypatch.setattr(pe, "AUDIT_JSONL", publishing / "audit.jsonl")
    monkeypatch.setattr(pe, "JOBS_DIR", publishing / "jobs")

    return {"tmp_path": tmp_path, "publishing": publishing, "decisions": decisions}


class TestPublishingEngineCore:
    def test_create_job_and_queue(self, publishing_env: dict[str, Any]) -> None:
        job = _create_job(
            content_id="W99",
            channel="website",
            requested_by="Krishna Founder",
            notes="queue me",
        )
        assert job["state"] == pe.STATE_PUBLISH_PENDING
        assert job["bundle_id"] == "W99"
        assert job["channel"] == "website"
        assert job["orchestration_only"] is True
        queue = pe.list_queue()
        assert any(j["job_id"] == job["job_id"] for j in queue)

    def test_create_requires_editorial_approval(
        self, publishing_env: dict[str, Any]
    ) -> None:
        with pytest.raises(LookupError, match="editorially approved"):
            _create_job(
                content_id="W01",
                channel="website",
                requested_by="Human A",
            )

    def test_status_transitions_website_placeholder(
        self, publishing_env: dict[str, Any]
    ) -> None:
        job = _create_job(
            content_id="W99",
            channel="website",
            requested_by="Human A",
        )
        published = pe.manual_publish(job["job_id"], requested_by="Human A")
        assert published["state"] == pe.STATE_FAILED
        assert published["publication_truth"] == "unproven"
        assert published["state"] != pe.STATE_PUBLISHED
        assert published["adapter_result"]["status"] == "PLACEHOLDER"
        assert published["adapter_result"]["rendering_performed"] is False
        assert published["adapter_result"]["website_engine_invoked"] is False
        assert published["adapter_result"]["ok"] is True

    def test_social_channel_not_implemented(
        self, publishing_env: dict[str, Any]
    ) -> None:
        job = _create_job(
            content_id="W99",
            channel="linkedin",
            requested_by="Human A",
        )
        result = pe.manual_publish(job["job_id"], requested_by="Human A")
        assert result["state"] == pe.STATE_FAILED
        assert result["adapter_result"]["status"] == "NOT_IMPLEMENTED"
        assert result["adapter_result"]["external_api_called"] is False

    def test_audit_fields(self, publishing_env: dict[str, Any]) -> None:
        job = _create_job(
            content_id="W99",
            channel="website",
            requested_by="Auditor",
            notes="n1",
        )
        pe.manual_publish(job["job_id"], requested_by="Auditor")
        audit = pe.load_audit_for_job(job["job_id"])
        assert len(audit) >= 2
        for row in audit:
            for key in (
                "bundle_id",
                "requested_by",
                "utc_timestamp",
                "channel",
                "state",
                "errors",
                "retry_count",
            ):
                assert key in row

    def test_retry_from_failed(self, publishing_env: dict[str, Any]) -> None:
        job = _create_job(
            content_id="W99",
            channel="twitter",
            requested_by="Human A",
        )
        pe.manual_publish(job["job_id"], requested_by="Human A")
        retried = pe.retry_job(job["job_id"], requested_by="Human A")
        assert retried["retry_count"] >= 1
        assert retried["state"] == pe.STATE_FAILED  # still not implemented
        assert "retry" in [h["state"] for h in retried["state_history"]]

    def test_cancel(self, publishing_env: dict[str, Any]) -> None:
        job = _create_job(
            content_id="W99",
            channel="website",
            requested_by="Human A",
        )
        cancelled = pe.cancel_job(job["job_id"], requested_by="Human A")
        assert cancelled["state"] == pe.STATE_CANCELLED
        with pytest.raises(ValueError, match="cancelled"):
            pe.manual_publish(job["job_id"], requested_by="Human A")

    def test_permissions_ai_blocked(self, publishing_env: dict[str, Any]) -> None:
        with pytest.raises(PermissionError):
            _create_job(
                content_id="W99",
                channel="website",
                requested_by="AI",
            )

    def test_invalid_channel(self, publishing_env: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="Unknown channel"):
            _create_job(
                content_id="W99",
                channel="tiktok",
                requested_by="Human A",
            )

    def test_duplicate_publish_requires_remote_proof(
        self, publishing_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job = _create_job(
            content_id="W99",
            channel="website",
            requested_by="Human A",
        )
        first = pe.manual_publish(job["job_id"], requested_by="Human A")
        assert first["state"] != pe.STATE_PUBLISHED
        second = pe.manual_publish(job["job_id"], requested_by="Human A")
        assert second["state"] != pe.STATE_PUBLISHED

        def _remote_ack(job_payload: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": True,
                "remote_write_acknowledged": True,
                "remote_object_id": "cms-object-1",
                "external_http": True,
                "rendering_performed": True,
                "http_status": 200,
            }

        monkeypatch.setitem(pe.ADAPTERS, "website", _remote_ack)
        fresh = _create_job(
            content_id="W99",
            channel="website",
            requested_by="Human A",
        )
        proved = pe.manual_publish(fresh["job_id"], requested_by="Human A")
        assert proved["state"] == pe.STATE_PUBLISHED
        assert proved["publication_truth"] == "remote_write_confirmed"
        assert proved["verification_status"] == "verification_pending"
        with pytest.raises(LookupError, match="already published"):
            pe.manual_publish(fresh["job_id"], requested_by="Human A")


class TestPublishingAPI:
    def test_api_create_publish_queue(
        self, cms_client: TestClient, publishing_env: dict[str, Any]
    ) -> None:
        r = cms_client.post(
            "/api/v1/publishing/jobs",
            json={
                "content_id": "W99",
                "channel": "website",
                "requested_by": "API Human",
                "notes": "via api",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "publish_pending"
        job_id = body["job_id"]

        q = cms_client.get("/api/v1/publishing/jobs")
        assert q.status_code == 200
        assert any(i["job_id"] == job_id for i in q.json()["items"])

        d = cms_client.get(f"/api/v1/publishing/{job_id}")
        assert d.status_code == 200
        assert d.json()["job"]["bundle_id"] == "W99"

        p = cms_client.post(
            f"/api/v1/publishing/{job_id}/publish",
            json={"requested_by": "API Human"},
        )
        assert p.status_code == 200
        body = p.json()
        assert body["state"] != "published"
        assert body["job"]["publication_truth"] == "unproven"

    def test_api_retry_cancel_permissions(
        self, cms_client: TestClient, publishing_env: dict[str, Any]
    ) -> None:
        job = _create_job(
            content_id="W99",
            channel="instagram",
            requested_by="Human A",
        )
        pe.manual_publish(job["job_id"], requested_by="Human A")

        bad = cms_client.post(
            f"/api/v1/publishing/{job['job_id']}/retry",
            json={"requested_by": "bot"},
        )
        assert bad.status_code == 403

        ok = cms_client.post(
            f"/api/v1/publishing/{job['job_id']}/retry",
            json={"requested_by": "Human B"},
        )
        assert ok.status_code == 200

        pending = _create_job(
            content_id="W99",
            channel="newsletter",
            requested_by="Human A",
        )
        c = cms_client.post(
            f"/api/v1/publishing/{pending['job_id']}/cancel",
            json={"requested_by": "Human A"},
        )
        assert c.status_code == 200
        assert c.json()["state"] == "cancelled"

    def test_channels_endpoint(
        self, cms_client: TestClient, publishing_env: dict[str, Any]
    ) -> None:
        r = cms_client.get("/api/v1/publishing/channels")
        assert r.status_code == 200
        names = {c["channel"] for c in r.json()["channels"]}
        assert names == {
            "website",
            "linkedin",
            "twitter",
            "instagram",
            "newsletter",
        }

    def test_editorial_api_unchanged(
        self, cms_client: TestClient, publishing_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Minimal readiness stubs so editorial readiness still responds
        monkeypatch.setattr(
            "runner_api_routers.content_studio._read_tracker",
            lambda: [
                {
                    "content_id": "W99",
                    "title": "t",
                    "status": "QA Passed",
                    "qa_status": "PASS",
                    "current_step": "",
                    "next_step": "",
                    "draft_path": "input/W99/04_Draft.md",
                    "qa_output_path": "",
                    "final_output_path": "",
                    "artifact_folder": "",
                }
            ],
        )
        monkeypatch.setattr(
            "runner_api_routers.content_studio._week_artifacts",
            lambda week_id: {
                "brief": False,
                "seo": False,
                "research": False,
                "draft": True,
                "final": False,
                "design": False,
                "social": False,
                "email": False,
                "checklist": False,
            },
        )
        monkeypatch.setattr(
            "runner_api_routers.editorial.PROJECT_ROOT",
            publishing_env["tmp_path"],
        )
        (publishing_env["tmp_path"] / "output" / "qa_reports").mkdir(
            parents=True, exist_ok=True
        )
        r = cms_client.get("/api/v1/editorial/readiness/W99")
        assert r.status_code == 200
        assert r.json()["ok"] is True


class TestPublishingUI:
    def test_queue_page(
        self, cms_client: TestClient, publishing_env: dict[str, Any]
    ) -> None:
        _create_job(
            content_id="W99",
            channel="website",
            requested_by="Human A",
        )
        r = cms_client.get("/publishing")
        assert r.status_code == 200
        assert 'data-testid="publishing-queue"' in r.text
        assert "W99" in r.text

    def test_detail_page(
        self, cms_client: TestClient, publishing_env: dict[str, Any]
    ) -> None:
        job = _create_job(
            content_id="W99",
            channel="website",
            requested_by="Human A",
        )
        r = cms_client.get(f"/publishing/{job['job_id']}")
        assert r.status_code == 200
        assert 'data-testid="publishing-detail"' in r.text
        assert 'data-testid="pub-publish"' in r.text
        assert 'data-testid="publishing-audit"' in r.text
