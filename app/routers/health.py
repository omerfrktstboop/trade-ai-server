"""Separate liveness and operational readiness endpoints."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings
from app.db.session import async_session_factory
from app.services.admin_config import is_kill_switch_enabled
from app.services.matriks_gateway import gateway_client
from app.services.scanner import scanner

router = APIRouter(tags=["Health"])
EXPECTED_MIGRATION = "20260713_04"


@router.get("/health")
@router.get("/health/live")
async def health_live() -> JSONResponse:
    return JSONResponse(
        content={
            "status": "ok",
            "service": settings.app_name,
            "version": settings.app_version,
        }
    )


@router.get("/health/ready")
async def health_ready() -> JSONResponse:
    checks: dict[str, dict] = {}
    ready = True
    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
            kill_switch = await is_kill_switch_enabled(session)
            migration = "development-create-all"
            if settings.is_production:
                migration = (
                    await session.execute(
                        text("SELECT version_num FROM alembic_version")
                    )
                ).scalar_one_or_none()
                if migration != EXPECTED_MIGRATION:
                    ready = False
            checks["database"] = {"ok": True}
            checks["migration"] = {
                "ok": not settings.is_production or migration == EXPECTED_MIGRATION,
                "version": migration,
                "expected": EXPECTED_MIGRATION,
            }
            checks["killSwitch"] = {"ok": True, "active": kill_switch}
    except Exception as exc:
        ready = False
        checks["database"] = {"ok": False, "error": str(exc)}

    try:
        gateway = await gateway_client.health()
        quote_ages = [
            float(age)
            for age in (gateway.get("quoteAgeSeconds") or {}).values()
            if age is not None
        ]
        depth_ages = [
            float(age)
            for age in (gateway.get("depthAgeSeconds") or {}).values()
            if age is not None
        ]
        position_age = gateway.get("positionSyncAgeSeconds")
        verification_age = gateway.get("accountVerificationAgeSeconds")
        order_limits = gateway.get("orderLimits") or {}
        account_ok = gateway.get("runtimeMode") != "DEMO_LIVE" or (
            gateway.get("testAutoOrderEnabled") is True
            and order_limits.get("demoAccountConfirmed") is True
            and verification_age is not None
            and float(verification_age) <= 5
        )
        gateway_ok = bool(gateway.get("ok")) and gateway.get("configStale") is False
        freshness_ok = (
            bool(quote_ages)
            and max(quote_ages) <= 15
            and bool(depth_ages)
            and max(depth_ages) <= 10
        )
        position_ok = position_age is not None and float(position_age) <= 90
        backlog = int(gateway.get("callbackOutboxBacklog") or 0)
        gateway_ready = (
            gateway_ok
            and freshness_ok
            and position_ok
            and account_ok
            and backlog < 1000
        )
        ready = ready and gateway_ready
        checks["gateway"] = {
            "ok": gateway_ok,
            "configVersion": gateway.get("configVersion"),
            "configAgeSeconds": gateway.get("configAgeSeconds"),
        }
        checks["marketData"] = {
            "ok": freshness_ok,
            "maxQuoteAgeSeconds": max(quote_ages) if quote_ages else None,
            "maxDepthAgeSeconds": max(depth_ages) if depth_ages else None,
        }
        checks["positions"] = {"ok": position_ok, "ageSeconds": position_age}
        checks["demoAccount"] = {
            "ok": account_ok,
            "verificationAgeSeconds": verification_age,
        }
        checks["callbacks"] = {
            "ok": backlog < 1000,
            "queueDepth": gateway.get("callbackQueueDepth"),
            "outboxBacklog": backlog,
        }
    except Exception as exc:
        ready = False
        checks["gateway"] = {"ok": False, "error": str(exc)}

    free_bytes = shutil.disk_usage(".").free
    disk_ok = free_bytes >= 1_000_000_000
    scanner_ok = not settings.scanner_enabled or scanner.running
    ready = ready and disk_ok and scanner_ok
    checks["disk"] = {"ok": disk_ok, "freeBytes": free_bytes}
    checks["scanner"] = {
        "ok": scanner_ok,
        "enabled": settings.scanner_enabled,
        "running": scanner.running,
        "allowOrders": settings.scanner_allow_orders,
    }
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "checkedAt": datetime.now(timezone.utc).isoformat(),
            "checks": checks,
        },
    )
