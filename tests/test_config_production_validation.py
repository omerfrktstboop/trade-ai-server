"""Production configuration validation tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def production_settings(**overrides: str) -> Settings:
    values = {
        "app_env": "production",
        "api_token": "legacy-development-token-not-used-123!",
        "evaluation_api_token": "eval-7F!k2P@q9Z#m4N$x8C&v1B*w6D+s3L",
        "gateway_api_token": "gateway-4M@r8T#y2Q!p7W$x5C&n9K*z1V",
        "admin_api_token": "admin-9Q!w3E@r7T#y1U$i5O&p8A*s2D",
        "admin_password": "Admin-6V!b2N@m8K#x4Z",
        "matriks_gateway_token": "matriks-5T!g9H@j3K#l7P$x1C&v8B*n",
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


def test_production_rejects_shared_scoped_tokens():
    shared = "shared-7F!k2P@q9Z#m4N$x8C&v1B*w6D+s3L"
    with pytest.raises(ValidationError, match="must be distinct"):
        production_settings(
            evaluation_api_token=shared,
            gateway_api_token=shared,
            admin_api_token=shared,
        )


def test_production_gateway_must_be_loopback():
    with pytest.raises(ValidationError, match="MATRIKS_GATEWAY_URL"):
        production_settings(matriks_gateway_url="http://10.0.0.2:8787")
