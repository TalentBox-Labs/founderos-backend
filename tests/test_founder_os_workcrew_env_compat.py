"""Phase 1 — FOUNDER_OS_* preferred over WORKCREW_* env compatibility."""

from __future__ import annotations

import os

import pytest

from src.tools.env_compat import (
    WORKCREW_ENV_SUFFIXES,
    getenv_compat,
    getenv_compat_optional,
    pop_compat,
    set_compat,
)


@pytest.fixture(autouse=True)
def _clean_crewai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for suffix in (
        "CREWAI_MODEL",
        "CREWAI_API_KEY",
        "CREWAI_TEMPERATURE",
        "OLLAMA_BASE_URL",
        "RUNTIME_CONFIG",
        "GOOGLE_SHEET_ID",
        "ALLOW_DIRECT_INPUT_WRITE",
    ):
        monkeypatch.delenv(f"FOUNDER_OS_{suffix}", raising=False)
        monkeypatch.delenv(f"WORKCREW_{suffix}", raising=False)


def test_founder_os_only_wins() -> None:
    env = {"FOUNDER_OS_CREWAI_MODEL": "founder-model"}
    assert getenv_compat("CREWAI_MODEL", "default", environ=env) == "founder-model"


def test_workcrew_legacy_only_fallback() -> None:
    env = {"WORKCREW_CREWAI_MODEL": "legacy-model"}
    assert getenv_compat("CREWAI_MODEL", "default", environ=env) == "legacy-model"


def test_both_present_founder_os_wins() -> None:
    env = {
        "FOUNDER_OS_CREWAI_MODEL": "founder-model",
        "WORKCREW_CREWAI_MODEL": "legacy-model",
    }
    assert getenv_compat("CREWAI_MODEL", "default", environ=env) == "founder-model"


def test_neither_present_uses_default() -> None:
    assert getenv_compat("CREWAI_MODEL", "ollama/llama3.1:8b", environ={}) == (
        "ollama/llama3.1:8b"
    )


def test_founder_os_empty_string_still_wins_over_legacy() -> None:
    env = {
        "FOUNDER_OS_GOOGLE_SHEET_ID": "",
        "WORKCREW_GOOGLE_SHEET_ID": "legacy-sheet",
    }
    assert getenv_compat("GOOGLE_SHEET_ID", "default", environ=env) == ""


def test_optional_none_when_absent() -> None:
    assert getenv_compat_optional("RUNTIME_CONFIG", environ={}) is None


def test_set_and_pop_compat_writes_both(monkeypatch: pytest.MonkeyPatch) -> None:
    set_compat("RUNTIME_CONFIG", "/tmp/overlay.json")
    assert os.environ["FOUNDER_OS_RUNTIME_CONFIG"] == "/tmp/overlay.json"
    assert os.environ["WORKCREW_RUNTIME_CONFIG"] == "/tmp/overlay.json"
    pop_compat("RUNTIME_CONFIG")
    assert "FOUNDER_OS_RUNTIME_CONFIG" not in os.environ
    assert "WORKCREW_RUNTIME_CONFIG" not in os.environ


def test_runtime_paths_prefers_founder_os(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from src.tools import runtime_paths as rp

    overlay = tmp_path / "overlay.json"
    overlay.write_text('{"active_week":"W99"}', encoding="utf-8")
    monkeypatch.setenv("FOUNDER_OS_RUNTIME_CONFIG", str(overlay))
    monkeypatch.setenv("WORKCREW_RUNTIME_CONFIG", "/should/not/win.json")
    cfg = rp.load_runtime_config()
    assert cfg["active_week"] == "W99"


def test_runtime_paths_legacy_workcrew_still_works(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from src.tools import runtime_paths as rp

    overlay = tmp_path / "legacy.json"
    overlay.write_text('{"active_week":"W42"}', encoding="utf-8")
    monkeypatch.setenv("WORKCREW_RUNTIME_CONFIG", str(overlay))
    cfg = rp.load_runtime_config()
    assert cfg["active_week"] == "W42"


def test_build_crew_llm_uses_founder_os_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock, patch

    monkeypatch.setenv("FOUNDER_OS_CREWAI_MODEL", "ollama/founder-test")
    monkeypatch.setenv("WORKCREW_CREWAI_MODEL", "ollama/legacy-test")
    with patch("src.crew.LLM", return_value=MagicMock()) as llm_cls:
        from src.crew import build_crew_llm

        build_crew_llm()
    kwargs = llm_cls.call_args.kwargs
    assert kwargs["model"] == "founder-test"


def test_build_crew_llm_legacy_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock, patch

    monkeypatch.setenv("WORKCREW_CREWAI_MODEL", "ollama/legacy-only")
    with patch("src.crew.LLM", return_value=MagicMock()) as llm_cls:
        from src.crew import build_crew_llm

        build_crew_llm()
    assert llm_cls.call_args.kwargs["model"] == "legacy-only"


def test_phase1_suffix_inventory_covers_active_readers() -> None:
    # Guard: known Phase 1 suffixes stay documented in the helper.
    required = {
        "CREWAI_MODEL",
        "CREWAI_API_KEY",
        "CREWAI_TEMPERATURE",
        "OLLAMA_BASE_URL",
        "RUNTIME_CONFIG",
        "GOOGLE_SHEET_ID",
        "GOOGLE_SERVICE_ACCOUNT",
        "ALLOW_DIRECT_INPUT_WRITE",
        "GENERATION_BACKUP",
    }
    assert required.issubset(set(WORKCREW_ENV_SUFFIXES))


def test_no_workcrew_env_vars_removed_from_inventory() -> None:
    # Phase 1 must not shrink the compatibility surface below the audited set.
    assert len(WORKCREW_ENV_SUFFIXES) >= 17
