"""Shared test fixtures and utilities."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
from fastapi.testclient import TestClient

# ── FastAPI TestClient Fixture ───────────────────────────────────────────────


@pytest.fixture
def cms_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    """TestClient for FastAPI CMS app with mocked dependencies."""
    from runner_api_routers.utils import _verify_api_key
    from runner_api import app

    # Override API key verification to always pass in tests
    async def mock_verify_api_key(token: str | None = None):
        return "test-key"

    app.dependency_overrides[_verify_api_key] = mock_verify_api_key

    yield TestClient(app)

    app.dependency_overrides.clear()


# ── Mock LLM Fixture ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_llm() -> MagicMock:
    """Mock CrewAI LLM for testing crews without external calls."""
    llm = MagicMock()
    llm.call = MagicMock(return_value="Mock LLM output")
    return llm


# ── Mock Crew Execution ──────────────────────────────────────────────────────


@pytest.fixture
def mock_crew_run(monkeypatch: pytest.MonkeyPatch):
    """Patch crew.kickoff() to return mocked results."""
    def fake_kickoff(self):
        return "# Mock output\nThis is test content."

    # This patches the Crew class's kickoff method globally
    from crewai import Crew
    monkeypatch.setattr(Crew, "kickoff", fake_kickoff)


# ── Test Data Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def fake_active_content(tmp_path: Path) -> dict[str, str]:
    """Fake tracker row; materializes a draft under tmp_path for crew unit tests."""
    draft = tmp_path / "input" / "W99" / "04_Draft.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("# Draft\n\nTest article body for crew unit tests.\n" * 5)

    return {
        "content_id": "W99",
        "title": "Test Content",
        "current_step": "Generation",
        "next_step": "QA",
        "draft_path": "input/W99/04_Draft.md",
        "final_path": "input/W99/05_Final.md",
        "qa_output_path": "output/qa_reports/W99_QA.md",
        "week": "W99",
        "artifact_folder": "W99",
    }


@pytest.fixture
def fake_runtime_config() -> dict[str, str]:
    """Fake runtime configuration for testing."""
    return {
        "active_week": "W99",
        "crewai_qa_source": "final",
        "final_path": "input/W99/05_Final.md",
        "draft_path": "input/W99/04_Draft.md",
        "qa_output_dir": "output/qa_reports/",
        "enable_crewai_qa": True,
        "validators": [
            "structure_checker",
            "metadata_checker",
            "draft_validator",
        ],
    }


@pytest.fixture
def fake_yaml_config() -> dict[str, dict]:
    """Fake YAML agent/task configuration (covers QA + generation key names)."""
    agent = {
        "role": "Test Agent",
        "goal": "Test goal",
        "backstory": "Test backstory",
    }
    task = {
        "description": "Test task description",
        "expected_output": "Test expected output",
    }
    return {
        "test_agent": agent,
        "test_task": task,
        "qa_agent": agent,
        "qa_review_task": task,
        "strategist_agent": agent,
        "seo_agent": agent,
        "research_agent": agent,
        "writer_agent": agent,
        "strategist_task": task,
        "seo_task": task,
        "research_task": task,
        "writer_task": task,
    }


# ── Monkeypatch Helpers ──────────────────────────────────────────────────────


@pytest.fixture
def patch_csv_reader(monkeypatch: pytest.MonkeyPatch, fake_active_content):
    """Patch CSV reader to return fake content (including import-bound names)."""
    import importlib

    def fake_get_active_content(content_id: str | None = None):
        return fake_active_content

    from src.tools import csv_reader

    monkeypatch.setattr(csv_reader, "get_active_content", fake_get_active_content)
    # Crews bind ``from src.tools.csv_reader import get_active_content`` at import time.
    for mod_name in (
        "src.qa_crew",
        "src.generation_crew",
        "src.editor_crew",
        "src.artifact_crew",
        "src.distribution_crew",
    ):
        mod = importlib.import_module(mod_name)
        if hasattr(mod, "get_active_content"):
            monkeypatch.setattr(mod, "get_active_content", fake_get_active_content)


@pytest.fixture
def patch_runtime_config(monkeypatch: pytest.MonkeyPatch, fake_runtime_config):
    """Patch runtime config loader (including import-bound names)."""
    import importlib

    def fake_load_runtime_config():
        return fake_runtime_config

    from src.tools import runtime_paths, csv_reader

    monkeypatch.setattr(runtime_paths, "load_runtime_config", fake_load_runtime_config)
    monkeypatch.setattr(csv_reader, "load_runtime_config", fake_load_runtime_config)
    for mod_name in (
        "src.qa_crew",
        "src.generation_crew",
        "src.editor_crew",
        "src.tools.runtime_paths",
    ):
        mod = importlib.import_module(mod_name)
        if hasattr(mod, "load_runtime_config"):
            monkeypatch.setattr(mod, "load_runtime_config", fake_load_runtime_config)


@pytest.fixture
def patch_yaml_loader(monkeypatch: pytest.MonkeyPatch, fake_yaml_config):
    """Patch BaseCrew YAML loader (instance method ``_load_yaml``)."""

    def fake_load_yaml(self, path: str):
        return fake_yaml_config

    from src.base_crew import BaseCrew

    monkeypatch.setattr(BaseCrew, "_load_yaml", fake_load_yaml)


# ── Temporary Directory Utilities ────────────────────────────────────────────


@pytest.fixture
def staging_dir(tmp_path: Path) -> Path:
    """Create a temporary staging directory structure."""
    staging = tmp_path / "staging" / "W99"
    staging.mkdir(parents=True, exist_ok=True)

    # Create some stub files
    (staging / "00_Generation_Seed.md").write_text("Seed content\n")
    (staging / "04_Draft.md").write_text("# Draft\nDraft content\n")

    return staging


@pytest.fixture
def repo_with_artifacts(tmp_path: Path) -> Path:
    """Create a temporary repo structure with week artifacts."""
    repo = tmp_path / "repo"
    weeks_dir = repo / "input" / "W99"
    weeks_dir.mkdir(parents=True, exist_ok=True)

    # Create week files
    (weeks_dir / "00_Generation_Seed.md").write_text("Seed\n")
    (weeks_dir / "01_Content_Brief.md").write_text("# Brief\n")
    (weeks_dir / "02_SEO_Plan.md").write_text("# SEO\n")
    (weeks_dir / "03_Research.md").write_text("# Research\n")
    (weeks_dir / "04_Draft.md").write_text("# Draft\n")
    (weeks_dir / "05_Final.md").write_text("# Final\n")

    # Create output dirs
    (repo / "output" / "qa_reports").mkdir(parents=True, exist_ok=True)

    return repo


def build_test_approval_request(**overrides):
    """Construct ApprovalRequest rows for tests with canonical identity fields."""
    from revenue_os.models.approvals import ApprovalRequest
    from revenue_os.services.approval_request_identity import (
        CAMPAIGN_FAMILIES,
        FAMILY_PROACTIVE_EMAIL,
        TARGET_TYPE_MARKETING_CAMPAIGN,
        build_logical_key,
        classify_approval_family,
        proactive_email_logical_key,
    )

    payload = dict(overrides.pop("payload", {}) or {})
    action_type = overrides.pop("action_type", "send_outreach_email")
    target_id = overrides.pop("target_id", None)
    target_type = overrides.pop("target_type", None)
    org_id = str(
        overrides.pop(
            "organization_id",
            payload.get("organization_id", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        )
    )
    payload.setdefault("organization_id", org_id)
    try:
        family_preview = classify_approval_family(action_type)
    except ValueError:
        family_preview = None
    if target_id:
        if family_preview in CAMPAIGN_FAMILIES:
            payload.setdefault("campaign_id", str(target_id))
            if target_type is None:
                target_type = TARGET_TYPE_MARKETING_CAMPAIGN
        else:
            payload.setdefault("contact_id", str(target_id))
            if target_type is None:
                target_type = "contact"
    try:
        family = classify_approval_family(action_type)
        logical_key = build_logical_key(
            approval_family=family,
            organization_id=org_id,
            target_id=str(target_id) if target_id else None,
            payload=payload,
            target_type=target_type,
        )
    except ValueError:
        family = FAMILY_PROACTIVE_EMAIL
        logical_key = proactive_email_logical_key(str(target_id or payload.get("contact_id", "x")))

    defaults = {
        "organization_id": org_id,
        "approval_family": family,
        "logical_key": logical_key,
        "requested_by": "test",
        "action_type": action_type,
        "title": "test approval",
        "description": "test",
        "status": "pending",
    }
    if target_id is not None:
        defaults["target_type"] = target_type or "contact"
        defaults["target_id"] = str(target_id)
    defaults.update(overrides)
    defaults["payload"] = payload
    return ApprovalRequest(**defaults)
