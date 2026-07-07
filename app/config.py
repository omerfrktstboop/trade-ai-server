"""Application configuration via environment variables.

Uses Pydantic Settings to load from .env file and environment.
Includes validation to prevent running in production without proper secrets.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnv(str, Enum):
    """Application environment."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class AIProvider(str, Enum):
    """Supported AI providers."""

    MOCK = "mock"
    DEEPSEEK = "deepseek"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class Mode(str, Enum):
    """Trading mode."""

    PAPER = "paper"
    LIVE = "live"
    DEMO_LIVE = "demo_live"
    REAL_LIVE = "real_live"


# ---------------------------------------------------------------------------
# Validators for production safety
# ---------------------------------------------------------------------------


def validate_api_token_not_empty(v: str) -> str:
    """Reject empty API token in production."""
    return v


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App metadata ──────────────────────────────────────────────────────

    app_name: str = "trade-ai-server"
    app_version: str = "0.1.0"
    app_env: AppEnv = AppEnv.DEVELOPMENT

    # ── Server ────────────────────────────────────────────────────────────

    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # ── CORS ──────────────────────────────────────────────────────────────

    cors_origins_raw: str = Field(
        default="*",
        alias="CORS_ORIGINS",
        description="Comma-separated list of allowed origins",
    )

    @property
    def cors_origins(self) -> list[str]:
        """Parse comma-separated CORS origins into a list."""
        return [o.strip() for o in self.cors_origins_raw.split(",") if o.strip()]

    # ── Security ──────────────────────────────────────────────────────────

    api_token: str = Field(
        default="dev-token-change-me",
        description="API authentication token. Default is for local dev only.",
    )
    admin_password: str = Field(
        default="admin-change-me",
        description="Admin panel password. Change for staging/production.",
    )

    # ── AI Provider ───────────────────────────────────────────────────────

    ai_provider: AIProvider = AIProvider.MOCK
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_timeout: int = 30

    # ── Database ──────────────────────────────────────────────────────────

    database_url: str = ""

    # ── Telegram ──────────────────────────────────────────────────────────

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── Trading ───────────────────────────────────────────────────────────

    default_mode: Mode = Mode.PAPER

    # ── Paths ─────────────────────────────────────────────────────────────

    base_dir: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent,
    )

    # ── Validation ────────────────────────────────────────────────────────

    @model_validator(mode="after")
    def _validate_production_safety(self) -> "Settings":
        """Block startup in production when secrets are missing or defaults are used."""
        if self.app_env != AppEnv.PRODUCTION:
            return self

        errors: list[str] = []

        # API token must be set and not the dev default
        if not self.api_token or self.api_token == "dev-token-change-me":
            errors.append(
                "API_TOKEN is empty or still set to dev default. "
                "Set a secure token in production."
            )
        if not self.admin_password or self.admin_password == "admin-change-me":
            errors.append(
                "ADMIN_PASSWORD is empty or still set to dev default. "
                "Set a secure admin password in production."
            )

        # AI provider key required
        if self.ai_provider == AIProvider.DEEPSEEK and not self.deepseek_api_key:
            errors.append("DEEPSEEK_API_KEY is required when AI_PROVIDER=deepseek")

        # Mock provider is not allowed in production
        if self.ai_provider == AIProvider.MOCK:
            errors.append(
                "AI_PROVIDER=mock is not allowed in production. "
                "Use deepseek, openai, or anthropic for live trading."
            )

        # Database: must be set and must NOT be SQLite in production
        if not self.database_url:
            errors.append(
                "DATABASE_URL is required in production. "
                "Use PostgreSQL (e.g. postgresql+asyncpg://...)."
            )
        elif self.database_url.startswith("sqlite"):
            errors.append(
                "DATABASE_URL must use PostgreSQL in production, not SQLite. "
                f"Got: {self.database_url}"
            )

        if errors:
            raise ValueError(
                "Production safety check failed:\n- " + "\n- ".join(errors)
            )

        return self

    @property
    def is_production(self) -> bool:
        """Convenience check for production environment."""
        return self.app_env == AppEnv.PRODUCTION

    @property
    def is_development(self) -> bool:
        """Convenience check for development environment."""
        return self.app_env == AppEnv.DEVELOPMENT


settings = Settings()
