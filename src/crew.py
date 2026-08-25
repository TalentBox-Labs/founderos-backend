import os
import sys
from pathlib import Path
from typing import Any

import yaml
from crewai import Agent, Crew, LLM, Process, Task

from src.crew_contract import qa_report_contract_errors
from src.tools.csv_reader import get_active_content
from src.tools.env_compat import getenv_compat
from src.tools.runtime_paths import load_runtime_config


BASE_DIR = Path(__file__).resolve().parent.parent

# Publish-ready final smaller than this falls back to draft (empty or stub file).
_MIN_CREWAI_FINAL_BYTES = 80


def resolve_crewai_qa_input_paths(
    *, repo_root: Path, runtime: dict[str, Any], active: dict[str, str]
) -> tuple[str, str]:
    """
    Choose which markdown file CrewAI QA should read.

    Returns ``(repo_relative_path, label_for_prompt)``.

    * Default ``crewai_qa_source`` is ``final``: use runtime ``final_path`` (typically
      ``05_Final.md``) when it exists and has body bytes; else fall back to the tracker
      ``draft_path``.
    * ``crewai_qa_source`` ``draft`` always uses ``draft_path`` (typically ``04_Draft.md``).
    """
    raw = str(runtime.get("crewai_qa_source", "final")).strip().lower()
    if raw not in ("final", "draft"):
        raw = "final"

    draft_rel = (active.get("draft_path") or runtime.get("draft_path") or "").strip()
    if raw == "draft":
        if not draft_rel:
            raise ValueError(
                "crewai_qa_source is draft but draft_path is missing on active row and runtime"
            )
        return draft_rel, f"working draft ({Path(draft_rel).name})"

    final_rel = (runtime.get("final_path") or "").strip()
    if final_rel:
        cand = repo_root / final_rel
        if cand.is_file() and cand.stat().st_size >= _MIN_CREWAI_FINAL_BYTES:
            return final_rel, f"publish candidate ({Path(final_rel).name})"

    if not draft_rel:
        raise ValueError(
            "crewai_qa_source is final but final_path is missing/empty and no draft_path fallback"
        )
    print(
        f"CrewAI QA: using draft fallback (final missing or < {_MIN_CREWAI_FINAL_BYTES} bytes): "
        f"final={final_rel!r} -> {draft_rel}",
        file=sys.stderr,
    )
    return (
        draft_rel,
        f"working draft ({Path(draft_rel).name}) — final missing or too small",
    )


def load_yaml(file_path: str) -> dict:
    with open(BASE_DIR / file_path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def read_file(file_path: str) -> str:
    full_path = BASE_DIR / file_path
    if not full_path.exists():
        raise FileNotFoundError(f"File not found: {full_path}")
    return full_path.read_text(encoding="utf-8")


def save_file(file_path: str, content: str) -> None:
    full_path = BASE_DIR / file_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")


def build_crew_llm() -> LLM:
    """LLM from environment (defaults: local Ollama)."""
    model_raw = getenv_compat("CREWAI_MODEL", "ollama/llama3.1:8b")
    base_url = getenv_compat(
        "OLLAMA_BASE_URL",
        os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
    )
    # Strip provider prefix (e.g. "ollama/") — bare model name is passed with explicit api_base
    model = model_raw.split("/", 1)[-1] if "/" in model_raw else model_raw
    api_key = getenv_compat("CREWAI_API_KEY", "ollama")
    temperature = float(getenv_compat("CREWAI_TEMPERATURE", "0"))
    return LLM(model=model, api_base=base_url, api_key=api_key, temperature=temperature)


def run_qa_agent(output_path: str | None = None) -> str:
    active = get_active_content()
    runtime = load_runtime_config()
    doc_path, doc_label = resolve_crewai_qa_input_paths(
        repo_root=BASE_DIR, runtime=runtime, active=active
    )
    article_body = read_file(doc_path)
    print(f"\nCrewAI QA — input: {doc_path} ({doc_label})\n", flush=True)

    agents_config = load_yaml("src/agents.yaml")
    tasks_config = load_yaml("src/tasks.yaml")

    llm = build_crew_llm()

    qa_agent = Agent(
        config=agents_config["qa_agent"],
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )

    qa_task = Task(
        config=tasks_config["qa_review_task"],
        agent=qa_agent,
        description=f"""
Review this WorkCrew article for structured QA.

Document: {doc_label}
Repository path: {doc_path}

Content ID: {active["content_id"]}
Week: {active.get("week", active["content_id"])}
Title: {active["title"]}
Current Step: {active["current_step"]}
Next Step: {active["next_step"]}

Article markdown (YAML frontmatter counts as observable text when present):
---
{article_body}
---

Hard failures (## Failed Checks ONLY for these; otherwise under ## Failed Checks write exactly `- (none)`):
- Quote a banned/forbidden phrase that appears verbatim in the article, OR
- Name a required heading/section from the task's ONLY validate list that is literally absent from the article body, OR
- Quote two identical full sentences that appear twice in the body (duplication).

If none of the above apply, ## Failed Checks must contain only `- (none)`.

Important rules:
- Use ONLY the visible text inside the supplied article.
- Never infer prior versions.
- Never estimate deleted words.
- Never assume editing history.
- Never generate hidden metrics.
- Never invent workflow context.
- Only report directly observable issues.
- If evidence is not explicitly present in the article, do not mention it.
- For a publish candidate (05_Final), base Passed/Failed/Observable only on that file.
- Never estimate or state word counts; ignore `word_count_target` in YAML for pass/fail.
- Put subjective or editorial notes only in ## Observable Issues; use ## Failed Checks only for hard breaches defined in the task template.
- Use FAIL in ## Final Verdict only when ## Failed Checks is not empty and not just `(none)`.
- Output only the required QA structure.
""",
        expected_output=tasks_config["qa_review_task"]["expected_output"],
    )

    crew = Crew(
        agents=[qa_agent],
        tasks=[qa_task],
        process=Process.sequential,
        verbose=True,
    )

    result = crew.kickoff()
    result_text = str(result)

    contract_errors = qa_report_contract_errors(result_text)
    if contract_errors:
        banner = (
            "<!-- CREWAI_QA_CONTRACT_VIOLATION\n"
            + "\n".join(f"- {e}" for e in contract_errors)
            + "\n-->\n\n"
        )
        result_text = banner + result_text
        print(
            "WARNING: CrewAI QA output did not meet markdown contract:\n"
            + "\n".join(contract_errors),
            file=sys.stderr,
        )

    # Allow Phase 2 pipeline to write an additional QA artifact without overwriting
    # validator-generated tracker artifacts.
    target = output_path or active["qa_output_path"]
    save_file(target, result_text)

    return result_text


if __name__ == "__main__":
    # Match pipeline_runner: never overwrite tracker `qa_output_path` (publish checklist artifact).
    cfg = load_runtime_config()
    week = str(cfg.get("active_week", "unknown"))
    qa_dir = str(cfg.get("qa_output_dir", "output/qa_reports/"))
    out_path = f"{qa_dir.rstrip('/')}/{week}_CrewAI_QA.md"
    output = run_qa_agent(output_path=out_path)
    print(f"\nQA REPORT GENERATED → {out_path}\n")
    print(output)