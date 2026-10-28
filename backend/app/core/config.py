import json
from functools import lru_cache
from typing import Any, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Application
    PROJECT_NAME: str = "LaTrobe Irrigation Brain API"
    VERSION: str = "0.1.0"
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        "https://app.latrobeirrigation.com.au",
    ]

    # Firebase Admin
    FIREBASE_CREDENTIALS_PATH: str = "credentials/serviceAccountKey.json"

    # Google Calendar API
    GOOGLE_SERVICE_ACCOUNT_FILE: str = "credentials/googleCalendarKey.json"
    GOOGLE_CALENDAR_ID: str = "marcoripa1512@gmail.com"

    # QuickBooks Online
    QUICKBOOKS_CLIENT_ID: str = ""
    QUICKBOOKS_CLIENT_SECRET: str = ""
    QUICKBOOKS_REDIRECT_URI: str = "http://localhost:8000/api/v1/quickbooks/callback"
    QUICKBOOKS_REALM_ID: str = ""
    QUICKBOOKS_ACCESS_TOKEN: str = ""
    QUICKBOOKS_REFRESH_TOKEN: str = ""
    QUICKBOOKS_ENVIRONMENT: Literal["sandbox", "production"] = "sandbox"

    # Telegram Bot & Webhook
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_ADMIN_CHAT_ID: str = ""
    TELEGRAM_WEBHOOK_SECRET: str = ""
    TELEGRAM_WEBHOOK_BASE_URL: str = ""
    TELEGRAM_USER_IDS: Any = []

    # LLM Configuration (for Telegram natural language intent parsing)
    GEMINI_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    LLM_MODEL: str = "gemini-3.8-flash"

    # Gmail IMAP / SMTP Integration
    GMAIL_ADDRESS: str = ""
    GMAIL_APP_PASSWORD: str = ""
    GMAIL_IMAP_HOST: str = "imap.gmail.com"
    GMAIL_SMTP_HOST: str = "smtp.gmail.com"
    GMAIL_SMTP_PORT: int = 587

    @field_validator("TELEGRAM_USER_IDS", mode="before")
    @classmethod
    def assemble_telegram_user_ids(cls, v: Any) -> list[int]:
        if isinstance(v, str):
            v_str = v.strip().strip('"').strip("'")
            if not v_str:
                return []
            if v_str.startswith("["):
                try:
                    return [int(x) for x in json.loads(v_str)]
                except Exception:
                    pass
            parts = [p.strip() for p in v_str.split(",") if p.strip()]
            return [int(p) for p in parts if p.isdigit() or (p.startswith("-") and p[1:].isdigit())]
        if isinstance(v, (list, tuple, set)):
            return [int(x) for x in v]
        if isinstance(v, int):
            return [v]
        return []

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            v_str = v.strip()
            if not v_str:
                return []
            if v_str.startswith("["):
                return json.loads(v_str)
            return [origin.strip() for origin in v_str.split(",") if origin.strip()]
        if isinstance(v, list):
            return v
        raise ValueError(f"Invalid CORS_ORIGINS format: {v}")


@lru_cache
def get_settings() -> Settings:
    """Return a cached instance of application settings."""
    return Settings()


settings = get_settings()
