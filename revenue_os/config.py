from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from revenue_os.env_compat import getenv_compat

load_dotenv()


@dataclass
class Settings:
    # Project
    PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
    DEBUG: bool = os.getenv("REVENUE_OS_DEBUG", "false").lower() == "true"

    # Database
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql://localhost:5432/revenue_os",
    )
    DATABASE_ECHO: bool = os.getenv("DATABASE_ECHO", "false").lower() == "true"

    # AI
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o")
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

    # Redis
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # ChromaDB
    CHROMA_PERSIST_DIR: str = str(
        PROJECT_ROOT / "data" / "chroma_db"
    )

    # Auth
    SECRET_KEY: str = os.getenv("SECRET_KEY", "")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(
        os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440")
    )

    def __post_init__(self):
        if not self.SECRET_KEY or self.SECRET_KEY == "change-me-in-production":
            raise ValueError(
                "FATAL: SECRET_KEY must be set to a strong random value. "
                "Set the SECRET_KEY environment variable before starting the application. "
                "Generate with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )

    # External integrations
    SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "")
    SLACK_SIGNING_SECRET: str = os.getenv("SLACK_SIGNING_SECRET", "")
    GMAIL_CREDENTIALS_PATH: str = os.getenv("GMAIL_CREDENTIALS_PATH", "")
    STRIPE_API_KEY: str = os.getenv("STRIPE_API_KEY", "")
    LINKEDIN_ACCESS_TOKEN: str = os.getenv("LINKEDIN_ACCESS_TOKEN", "")
    WHATSAPP_API_TOKEN: str = os.getenv("WHATSAPP_API_TOKEN", "")
    WHATSAPP_PHONE_NUMBER_ID: str = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")

    # n8n
    N8N_WEBHOOK_BASE_URL: str = os.getenv(
        "N8N_WEBHOOK_BASE_URL", "http://localhost:5678/webhook"
    )
    N8N_API_KEY: str = os.getenv("N8N_API_KEY", "")

    # Google Sheets — FOUNDER_OS_* preferred; WORKCREW_* legacy fallback
    GOOGLE_SERVICE_ACCOUNT: str = getenv_compat("GOOGLE_SERVICE_ACCOUNT", "")
    GOOGLE_SHEET_ID: str = getenv_compat("GOOGLE_SHEET_ID", "")

    # CrewAI — FOUNDER_OS_* preferred; WORKCREW_* legacy fallback
    # Attribute name retained for compatibility with existing callers.
    WORKCREW_CREWAI_MODEL: str = getenv_compat(
        "CREWAI_MODEL", "ollama/llama3.1:8b"
    )


settings = Settings()
