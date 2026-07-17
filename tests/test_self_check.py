"""Read-only operational readiness self-check tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import AppEnv, AIProvider
from app.services import self_check
from app.services.matriks_gateway import GatewayUnavailable


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, *_args, **_kwargs):
        return None


@pytest.fixture
def isolated_self_check(monkeypatch):
    monkeypatch.setattr(self_check, "async_session_factory", lambda: _Session())

    async def profile(_session):
        return SimpleNamespace(code="NORMAL")

    async def system_mode(_session):
        return "OBSERVE_ONLY"

    async def disabled(_session):
        return False

    monkeypatch.setattr(self_check, "get_active_profile", profile)
    monkeypatch.setattr(self_check, "get_system_mode", system_mode)
    # Panel-over-env okuyucular gerçek session ister; sahte _Session yerine
    # doğrudan kapalı değer döndür.
    monkeypatch.setattr(self_check, "is_scanner_runtime_enabled", disabled)
    monkeypatch.setattr(self_check.settings, "app_env", AppEnv.DEVELOPMENT)
    monkeypatch.setattr(self_check.settings, "matriks_gateway_token", "")
    monkeypatch.setattr(self_check.settings, "scanner_enabled", False)
    monkeypatch.setattr(self_check.settings, "ai_provider", AIProvider.MOCK)


@pytest.mark.asyncio
async def test_missing_gateway_token_warns(isolated_self_check, monkeypatch):
    async def health():
        return {"positionsLoaded": True}

    monkeypatch.setattr(self_check.gateway_client, "health", health)
    result = await self_check.run_self_check()
    checks = {item["name"]: item for item in result["checks"]}

    assert checks["gateway-token"]["status"] == "WARN"
    # v2: manuel onay kapısı kaldırıldı; config check systemMode gösterir.
    assert "systemMode=" in checks["admin-config"]["message"]
    assert "manual-approval-order-gate" not in checks


@pytest.mark.asyncio
async def test_gateway_unavailable_does_not_prevent_response(
    isolated_self_check, monkeypatch
):
    async def unavailable():
        raise GatewayUnavailable("gateway offline")

    monkeypatch.setattr(self_check.gateway_client, "health", unavailable)
    result = await self_check.run_self_check()
    checks = {item["name"]: item for item in result["checks"]}

    assert checks["gateway-health"]["status"] == "FAIL"
    assert "gateway offline" in checks["gateway-health"]["message"]


@pytest.mark.asyncio
async def test_production_missing_token_and_unsupported_provider_fail(
    isolated_self_check, monkeypatch
):
    async def health():
        return {"positionsLoaded": True}

    monkeypatch.setattr(self_check.gateway_client, "health", health)
    monkeypatch.setattr(self_check.settings, "app_env", AppEnv.PRODUCTION)
    monkeypatch.setattr(self_check.settings, "ai_provider", "openai")
    result = await self_check.run_self_check()
    checks = {item["name"]: item for item in result["checks"]}

    assert checks["gateway-token"]["status"] == "FAIL"
    assert checks["ai-provider"]["status"] == "FAIL"
