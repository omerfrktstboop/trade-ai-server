"""Read-only production readiness checks. Never sends an order."""

from __future__ import annotations
import time
from typing import Any
from sqlalchemy import text
from app.config import is_supported_ai_provider, settings
from app.db.session import async_session_factory
from app.services.admin_config import (
    get_system_mode,
    is_scanner_runtime_enabled,
)
from app.services.matriks_gateway import (
    gateway_client,
)
from app.services.scanner import scanner
from app.services.trade_profile import get_active_profile


async def run_self_check() -> dict[str, Any]:
    checks = []

    async def check(name, action):
        started = time.perf_counter()
        try:
            message, status = await action()
        except Exception as exc:
            message, status = str(exc), "FAIL"
        checks.append(
            {
                "name": name,
                "status": status,
                "message": message,
                "durationMs": round((time.perf_counter() - started) * 1000, 1),
            }
        )

    async def db():
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
        return "Database connection OK", "PASS"

    async def config():
        async with async_session_factory() as session:
            profile = await get_active_profile(session)
            system_mode = await get_system_mode(session)
        return f"profile={profile.code} systemMode={system_mode}", "PASS"

    async def gateway():
        health = await gateway_client.health()
        return f"positionsLoaded={health.get('positionsLoaded')}", "PASS" if health.get(
            "positionsLoaded"
        ) else "WARN"

    async def gateway_token():
        configured = bool(settings.matriks_gateway_token.strip())
        if configured:
            return "Gateway token configured", "PASS"
        return "Gateway token missing", "FAIL" if settings.is_production else "WARN"

    async def scanner_status():
        async with async_session_factory() as session:
            runtime_enabled = await is_scanner_runtime_enabled(session)
            system_mode = await get_system_mode(session)
        return (
            f"envEnabled={settings.scanner_enabled} panelEnabled={runtime_enabled} "
            f"running={scanner.running} systemMode={system_mode}",
            "PASS" if scanner.running and runtime_enabled else "WARN",
        )

    async def ai():
        provider = getattr(settings.ai_provider, "value", str(settings.ai_provider))
        if not is_supported_ai_provider(provider):
            return (
                f"Unsupported AI provider: {provider}. Supported providers: mock, deepseek",
                "FAIL" if settings.is_production else "WARN",
            )
        if provider == "mock" and settings.is_production:
            return "AI_PROVIDER=mock is not allowed in production", "FAIL"
        return f"provider={provider}", "PASS"

    await check("database", db)
    await check("admin-config", config)
    await check("ai-provider", ai)
    await check("gateway-health", gateway)
    await check("gateway-token", gateway_token)
    await check("scanner", scanner_status)
    return {
        "status": "FAIL"
        if any(c["status"] == "FAIL" for c in checks)
        else "WARN"
        if any(c["status"] == "WARN" for c in checks)
        else "PASS",
        "checks": checks,
    }
