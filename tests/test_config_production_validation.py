"""Production configuration validation tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def production_settings(**overrides: str) -> Settings:
    values = {
        "app_env": "production",
        "api_token": "a-strong-api-token",
        "admin_password": "a-strong-admin-password",
        "matriks_gateway_token": "a-strong-gateway-token",
        "ai_provider": "deepseek",
        "deepseek_api_key": "deepseek-key",
        "database_url": "postgresql+asyncpg://trade_ai:secret@localhost:5432/trade_ai",
        "CORS_ORIGINS": "https://admin.example.test",
    }
    values.update(overrides)
    return Settings(**values)


def test_production_requires_matriks_gateway_token():
    with pytest.raises(ValidationError, match="MATRIKS_GATEWAY_TOKEN"):
        production_settings(matriks_gateway_token="")


def test_openai_is_rejected_as_unsupported_provider():
    with pytest.raises(ValidationError, match="Supported providers: mock, deepseek"):
        production_settings(ai_provider="openai")


def test_valid_deepseek_postgresql_production_settings_pass():
    settings = production_settings()
    assert settings.ai_provider.value == "deepseek"
