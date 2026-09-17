from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from revenue_os.db_url import validate_database_url, validate_secret_key

load_dotenv()


@dataclass
class Settings:
    # Project
    PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
    DEBUG: bool = os.getenv("REVENUE_OS_DEBUG", "false").lower() == "true"

    # Database — no silent localhost default; missing/invalid fails closed at import.
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
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
        self.SECRET_KEY = validate_secret_key(self.SECRET_KEY)
        self.DATABASE_URL = validate_database_url(self.DATABASE_URL)

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

    # Google Sheets (reuse existing CMS OS vars)
    GOOGLE_SERVICE_ACCOUNT: str = os.getenv(
        "WORKCREW_GOOGLE_SERVICE_ACCOUNT", ""
    )
    GOOGLE_SHEET_ID: str = os.getenv("WORKCREW_GOOGLE_SHEET_ID", "")

    # CrewAI (reuse existing CMS OS defaults)
    WORKCREW_CREWAI_MODEL: str = os.getenv(
        "WORKCREW_CREWAI_MODEL", "ollama/llama3.1:8b"
    )


settings = Settings()
