"""Application configuration via environment variables."""

from pathlib import Path
from typing import Annotated

from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_cors(v: str) -> list[str]:
    """Parse comma-separated CORS origins into a list."""
    if isinstance(v, list):
        return v
    return [origin.strip() for origin in v.split(",") if origin.strip()]


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App metadata
    app_name: str = "trade-ai-server"
    app_version: str = "0.1.0"

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # CORS
    cors_origins: Annotated[list[str], BeforeValidator(parse_cors)] = Field(
        default=["*"],
        description="Comma-separated list of allowed origins",
    )

    # Paths
    base_dir: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent,
    )

    # Database (placeholder — not yet connected)
    database_url: str = ""


settings = Settings()
