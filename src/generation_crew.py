"""Phase 2A generation crew using BaseCrew pattern."""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from crewai import Agent, Crew, Process, Task

from src.base_crew import BaseCrew
from src.crew import BASE_DIR, build_crew_llm, load_yaml, read_file, save_file
from src.tools.csv_reader import get_active_content
from src.tools.env_compat import getenv_compat
from src.tools.runtime_paths import load_runtime_config
from src.tools.staging_overlay import warn_if_staging_week_mismatch

logger = logging.getLogger(__name__)


class GenerationCrew(BaseCrew):
    """Phase 2A: Strategist → SEO → Research → Writer crew."""

    def __init__(self, repo_root: Path | None = None):
        """Initialize generation crew."""
        super().__init__(
            name="generation",
            agents_yaml_path="src/agents_generation.yaml",
            tasks_yaml_path="src/tasks_generation.yaml",
            repo_root=repo_root,
        )
        self.output_root: str | None = None
        self.content_id: str | None = None
        self.active: dict[str, Any] = {}
        self.paths: dict[str, str] = {}

    def build_agents_and_tasks(self) -> tuple[list[Agent], list[Task]]:
        """Build generation crew agents and tasks."""
        # Load content and paths
        self.active = get_active_content(self.content_id)
        week = str(self.active["content_id"])
        self.paths = self._artifact_paths_for_run(self.active, self.output_root)

        if self.output_root:
            logger.info(
                f"Generation artifacts → {self.output_root}",
                extra={"week": week, "output_root": self.output_root},
            )
            warn_if_staging_week_mismatch(self.output_root, week)

        # Load optional seed
        seed_text = self._load_optional_seed(self.paths["seed"])
        seed_block = (
            f"\n\n## Generation seed (optional)\n\n{seed_text}\n" if seed_text else ""
        )

        # Keys must match src/agents_generation.yaml + src/tasks_generation.yaml
        strategist = Agent(
            config=self.agents_config.get("strategist_agent", {}),
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )
        seo_specialist = Agent(
            config=self.agents_config.get("seo_agent", {}),
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )
        researcher = Agent(
            config=self.agents_config.get("research_agent", {}),
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )
        writer = Agent(
            config=self.agents_config.get("writer_agent", {}),
            llm=self.llm,
            verbose=True,
            allow_delegation=False,
        )

        strategist_task = Task(
            config=self.tasks_config.get("strategist_task", {}),
            agent=strategist,
            description=f"""Create content brief for: {self.active.get("title", "")}\n{seed_block}""",
        )

        seo_task = Task(
            config=self.tasks_config.get("seo_task", {}),
            agent=seo_specialist,
            description=f"""Plan SEO for: {self.active.get("title", "")}\n{seed_block}""",
        )

        research_task = Task(
            config=self.tasks_config.get("research_task", {}),
            agent=researcher,
            description=f"""Research topic: {self.active.get("title", "")}\n{seed_block}""",
        )

        writer_task = Task(
            config=self.tasks_config.get("writer_task", {}),
            agent=writer,
            description=f"""Write draft for: {self.active.get("title", "")}\n{seed_block}""",
        )

        return (
            [strategist, seo_specialist, researcher, writer],
            [strategist_task, seo_task, research_task, writer_task],
        )

    def validate_output(self, output: str) -> tuple[bool, list[str]]:
        """Validate generation output."""
        # Generation crew output is valid if it contains content
        errors = []
        if not output or len(output.strip()) < 100:
            errors.append("Generated content is too short")
        return len(errors) == 0, errors

    def run_generation(
        self,
        content_id: str | None = None,
        output_root: str | None = None,
        write_direct: bool = False,
    ) -> dict[str, str]:
        """
        Run generation crew and save artifacts.

        Args:
            content_id: Content ID to generate (defaults to active from tracker)
            output_root: Output directory for generated files
            write_direct: If True, write directly to input/ (requires env var)

        Returns:
            Mapping of step_name -> output text
        """
        self.content_id = content_id
        self.output_root = output_root

        # Validate write permissions
        if not output_root and not write_direct:
            raise RuntimeError(
                "Refusing to write into input/WXX/: use output_root or set "
                "WORKCREW_ALLOW_DIRECT_INPUT_WRITE=1"
            )

        # Run the crew
        result_text = self.run()

        # Save artifacts
        steps = [
            ("brief", self.paths["brief"]),
            ("seo", self.paths["seo"]),
            ("research", self.paths["research"]),
            ("draft", self.paths["draft"]),
        ]

        results = {}
        for step_name, output_path in steps:
            # Back up existing file
            self._maybe_backup(output_path)
            # Save generated content
            self.save_file(output_path, result_text)
            results[step_name] = result_text

            logger.info(
                f"Saved {step_name}",
                extra={"path": output_path, "week": self.active.get("content_id")},
            )

        return results

    def _artifact_paths_for_run(
        self, active: dict[str, Any], output_root: str | None
    ) -> dict[str, str]:
        """Determine artifact paths for this run."""
        root = Path(active["draft_path"]).parent
        base = {
            "brief": str(root / "01_Content_Brief.md"),
            "seo": str(root / "02_SEO_Plan.md"),
            "research": str(root / "03_Research.md"),
            "draft": str(root / "04_Draft.md"),
            "seed": str(root / "00_Generation_Seed.md"),
        }

        if not output_root:
            return base

        out = Path(output_root)
        return {
            "brief": str(out / "01_Content_Brief.md"),
            "seo": str(out / "02_SEO_Plan.md"),
            "research": str(out / "03_Research.md"),
            "draft": str(out / "04_Draft.md"),
            "seed": base["seed"],  # Always read seed from original
        }

    def _maybe_backup(self, repo_relative_path: str) -> None:
        """Back up file if WORKCREW_GENERATION_BACKUP is enabled."""
        if getenv_compat("GENERATION_BACKUP", "1").lower() in (
            "0",
            "false",
            "no",
        ):
            return
        full = self.repo_root / repo_relative_path
        if not full.is_file():
            return
        bak = full.with_suffix(full.suffix + ".bak")
        shutil.copy2(full, bak)
        logger.info(f"Backed up: {bak.relative_to(self.repo_root)}")

    def _load_optional_seed(self, seed_path: str) -> str:
        """Load optional generation seed file."""
        full = self.repo_root / seed_path
        if not full.is_file():
            return ""
        return full.read_text(encoding="utf-8").strip()


# ── Procedural Phase 2A API (tests, pipeline subprocess compatibility) ───────


def _week_paths_from_draft(draft_path: str) -> dict[str, str]:
    root = Path(draft_path).parent
    return {
        "brief": str(root / "01_Content_Brief.md"),
        "seo": str(root / "02_SEO_Plan.md"),
        "research": str(root / "03_Research.md"),
        "draft": str(root / "04_Draft.md"),
        "seed": str(root / "00_Generation_Seed.md"),
    }


def _artifact_paths_for_run(
    active: dict[str, Any],
    output_root: str | None,
) -> dict[str, str]:
    base = _week_paths_from_draft(active["draft_path"])
    if not output_root:
        return base
    out = Path(output_root)
    return {
        "brief": str(out / "01_Content_Brief.md"),
        "seo": str(out / "02_SEO_Plan.md"),
        "research": str(out / "03_Research.md"),
        "draft": str(out / "04_Draft.md"),
        "seed": base["seed"],
    }


def _maybe_backup(repo_relative_path: str) -> None:
    if getenv_compat("GENERATION_BACKUP", "1").lower() in (
        "0",
        "false",
        "no",
    ):
        return
    full = BASE_DIR / repo_relative_path
    if not full.is_file():
        return
    bak = full.with_suffix(full.suffix + ".bak")
    shutil.copy2(full, bak)
    print(f"Backed up -> {bak.relative_to(BASE_DIR)}", file=sys.stderr)


def _load_optional_seed(seed_path: str) -> str:
    full = BASE_DIR / seed_path
    if not full.is_file():
        return ""
    return full.read_text(encoding="utf-8").strip()


def _run_single_task(
    *,
    agent_cfg: dict,
    description: str,
    expected_output: str,
    llm,
) -> str:
    agent = Agent(
        config=agent_cfg,
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )
    task = Task(
        description=description,
        expected_output=expected_output,
        agent=agent,
    )
    crew = Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=True,
    )
    return str(crew.kickoff())


def _direct_input_write_allowed() -> bool:
    return getenv_compat("ALLOW_DIRECT_INPUT_WRITE", "").lower() in (
        "1",
        "true",
        "yes",
    )


def run_phase_2a_chain(
    content_id: str | None = None,
    output_root: str | None = None,
) -> dict[str, str]:
    """Run Strategist → SEO → Research → Writer for one week (procedural API)."""
    if not output_root and not _direct_input_write_allowed():
        raise RuntimeError(
            "Refusing to write into input/WXX/: use --output-root (e.g. "
            "output/generated/W05) for staging generation, or set "
            "WORKCREW_ALLOW_DIRECT_INPUT_WRITE=1 only as an explicit escape hatch."
        )

    cfg_runtime = load_runtime_config()
    active = get_active_content(content_id)
    week = str(active["content_id"])
    paths = _artifact_paths_for_run(active, output_root)
    if output_root:
        print(
            f"Writing artifacts under {output_root} (input/ tree untouched).\n",
            file=sys.stderr,
        )
        warn_if_staging_week_mismatch(output_root, week)

    agents_y = load_yaml("src/agents_generation.yaml")
    tasks_y = load_yaml("src/tasks_generation.yaml")
    llm = build_crew_llm()

    seed_text = _load_optional_seed(paths["seed"])
    seed_block = (
        f"\n\n## Generation seed (optional)\n\n{seed_text}\n"
        if seed_text
        else "\n\n(No 00_Generation_Seed.md — infer topic from tracker title below.)\n"
    )

    meta = f"""Tracker metadata (read-only context):
- content_id: {active.get("content_id")}
- title label: {active.get("title")}
- current_step: {active.get("current_step")}
- next_step: {active.get("next_step")}
{seed_block}
"""

    results: dict[str, str] = {}

    desc_s = f"""{meta}
Produce the COMPLETE markdown body for `01_Content_Brief.md` for week {week}.
"""
    _maybe_backup(paths["brief"])
    out_brief = _run_single_task(
        agent_cfg=agents_y["strategist_agent"],
        description=desc_s,
        expected_output=tasks_y["strategist_task"]["expected_output"],
        llm=llm,
    )
    save_file(paths["brief"], out_brief)
    results["brief"] = out_brief
    brief_src = read_file(paths["brief"])

    desc_seo = f"""You have the approved brief below. Produce the COMPLETE markdown for `02_SEO_Plan.md`.

--- BEGIN BRIEF ---
{brief_src}
--- END BRIEF ---
"""
    _maybe_backup(paths["seo"])
    out_seo = _run_single_task(
        agent_cfg=agents_y["seo_agent"],
        description=desc_seo,
        expected_output=tasks_y["seo_task"]["expected_output"],
        llm=llm,
    )
    save_file(paths["seo"], out_seo)
    results["seo"] = out_seo
    seo_src = read_file(paths["seo"])

    desc_r = f"""Using the brief and SEO plan below, produce `03_Research.md`.

--- BEGIN BRIEF ---
{brief_src}
--- END BRIEF ---

--- BEGIN SEO PLAN ---
{seo_src}
--- END SEO PLAN ---
"""
    _maybe_backup(paths["research"])
    out_res = _run_single_task(
        agent_cfg=agents_y["research_agent"],
        description=desc_r,
        expected_output=tasks_y["research_task"]["expected_output"],
        llm=llm,
    )
    save_file(paths["research"], out_res)
    results["research"] = out_res
    res_src = read_file(paths["research"])

    desc_w = f"""Write `04_Draft.md` using all prior artifacts.

--- BEGIN BRIEF ---
{brief_src}
--- END BRIEF ---

--- BEGIN SEO PLAN ---
{seo_src}
--- END SEO PLAN ---

--- BEGIN RESEARCH ---
{res_src}
--- END RESEARCH ---
"""
    _maybe_backup(paths["draft"])
    out_draft = _run_single_task(
        agent_cfg=agents_y["writer_agent"],
        description=desc_w,
        expected_output=tasks_y["writer_task"]["expected_output"],
        llm=llm,
    )
    save_file(paths["draft"], out_draft)
    results["draft"] = out_draft

    print(
        f"\nPhase 2A complete for {week}. "
        f"runtime active_week in config is still {cfg_runtime.get('active_week')} "
        "(unchanged by design).\n",
        file=sys.stderr,
    )
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run Phase 2A generation crew")
    parser.add_argument("topic", nargs="?", default=None, help="Content topic/ID")
    parser.add_argument(
        "--output-root",
        default=None,
        help="Output directory for generated files",
    )
    parser.add_argument(
        "--write-direct",
        action="store_true",
        help="Write directly to input/ (requires WORKCREW_ALLOW_DIRECT_INPUT_WRITE)",
    )
    args = parser.parse_args()

    if args.write_direct:
        if not _direct_input_write_allowed():
            raise SystemExit(
                "ERROR: --write-direct requires WORKCREW_ALLOW_DIRECT_INPUT_WRITE=1"
            )
        crew = GenerationCrew()
        results = crew.run_generation(
            content_id=args.topic,
            output_root=None,
            write_direct=True,
        )
    else:
        # Procedural chain writes distinct brief/seo/research/draft artifacts.
        results = run_phase_2a_chain(
            content_id=args.topic,
            output_root=args.output_root,
        )

    print("\n" + "=" * 80)
    print("GENERATION CREW RESULTS")
    print("=" * 80)
    for step, content in results.items():
        print(f"\n{step.upper()}:\n{content[:200]}...\n")
