"""Base crew class with standardized patterns for all crew implementations."""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import yaml
from crewai import Agent, Crew, LLM, Process, Task

from src.tools.env_compat import getenv_compat

logger = logging.getLogger(__name__)


class BaseCrew(ABC):
    """
    Abstract base class for all CrewAI implementations.

    Provides:
    - Standardized LLM initialization
    - YAML configuration loading
    - Consistent error handling and logging
    - Output validation hooks
    - Unified crew execution pattern
    """

    def __init__(
        self,
        name: str,
        agents_yaml_path: str,
        tasks_yaml_path: str,
        repo_root: Path | None = None,
    ):
        """
        Initialize crew with configuration.

        Args:
            name: Crew identifier for logging
            agents_yaml_path: Path to agents config (relative to repo root)
            tasks_yaml_path: Path to tasks config (relative to repo root)
            repo_root: Repository root path (defaults to parent of src/)
        """
        self.name = name
        self.repo_root = repo_root or Path(__file__).resolve().parent.parent
        self.agents_yaml_path = agents_yaml_path
        self.tasks_yaml_path = tasks_yaml_path
        self.llm = self._build_llm()
        self.agents_config = self._load_yaml(agents_yaml_path)
        self.tasks_config = self._load_yaml(tasks_yaml_path)
        logger.info(f"Initialized {self.name} crew", extra={"crew": self.name})

    def _build_llm(self) -> LLM:
        """Build LLM instance from environment variables."""
        model_raw = getenv_compat("CREWAI_MODEL", "ollama/llama3.1:8b")
        base_url = getenv_compat(
            "OLLAMA_BASE_URL",
            os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        )
        # Strip provider prefix (e.g., "ollama/") — bare model name with explicit api_base
        model = model_raw.split("/", 1)[-1] if "/" in model_raw else model_raw
        api_key = getenv_compat("CREWAI_API_KEY", "ollama")
        temperature = float(getenv_compat("CREWAI_TEMPERATURE", "0"))

        logger.debug(
            f"Building LLM for {self.name}",
            extra={"model": model, "api_base": base_url},
        )
        return LLM(model=model, api_base=base_url, api_key=api_key, temperature=temperature)

    def _load_yaml(self, file_path: str) -> dict:
        """Load YAML configuration file."""
        full_path = self.repo_root / file_path
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError as e:
            logger.error(f"Config file not found: {full_path}", extra={"crew": self.name})
            raise

    def read_file(self, file_path: str) -> str:
        """Read file relative to repo root."""
        full_path = self.repo_root / file_path
        if not full_path.exists():
            logger.warning(f"File not found: {file_path}", extra={"crew": self.name})
            raise FileNotFoundError(f"File not found: {full_path}")
        return full_path.read_text(encoding="utf-8")

    def save_file(self, file_path: str, content: str) -> None:
        """Save file relative to repo root."""
        full_path = self.repo_root / file_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        logger.info(f"Saved output: {file_path}", extra={"crew": self.name})

    @abstractmethod
    def build_agents_and_tasks(self) -> tuple[list[Agent], list[Task]]:
        """
        Build and return agents and tasks for this crew.

        Must be implemented by subclasses.

        Returns:
            Tuple of (agents_list, tasks_list)
        """
        pass

    @abstractmethod
    def validate_output(self, output: str) -> tuple[bool, list[str]]:
        """
        Validate crew output against contract/requirements.

        Returns:
            Tuple of (is_valid, list_of_errors)
        """
        pass

    def run(self) -> str:
        """
        Execute crew workflow with standardized error handling.

        Returns:
            Crew output as string
        """
        try:
            logger.info(f"Starting {self.name} crew execution", extra={"crew": self.name})

            agents, tasks = self.build_agents_and_tasks()

            crew = Crew(
                agents=agents,
                tasks=tasks,
                process=Process.sequential,
                verbose=True,
            )

            result = crew.kickoff()
            result_text = str(result)

            # Validate output
            is_valid, errors = self.validate_output(result_text)
            if not is_valid:
                logger.warning(
                    f"{self.name} output validation failed",
                    extra={"crew": self.name, "errors": errors},
                )
                # Add validation errors as comment
                banner = (
                    f"<!-- {self.name.upper()}_VALIDATION_ERRORS\n"
                    + "\n".join(f"- {e}" for e in errors)
                    + "\n-->\n\n"
                )
                result_text = banner + result_text
            else:
                logger.info(
                    f"{self.name} crew completed successfully",
                    extra={"crew": self.name},
                )

            return result_text

        except Exception as e:
            logger.error(
                f"{self.name} crew failed: {str(e)}",
                extra={"crew": self.name},
                exc_info=True,
            )
            raise

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(name={self.name})>"
