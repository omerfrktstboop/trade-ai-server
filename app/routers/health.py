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
EXPECTED_MIGRATION = "20260717_14"


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
        # v2: dispatch yalnızca AUTO_TRADE'de mümkün, o yüzden hesap kimliği
        # yalnızca o modda readiness'i etkiler. Eskiden burada runtimeMode ==
        # "DEMO_LIVE" karşılaştırması vardı; cutover'da o kavram kalktığı için
        # koşul her zaman doğruya düşüyor ve kontrol sessizce ölmüştü.
        # CheckDispatchGates ile aynı kuralları yansıtır: hesap türü bilinmeli,
        # doğrulama taze olmalı, REAL ise arming şart.
        system_mode = str(gateway.get("systemMode") or "OBSERVE_ONLY").upper()
        account_type = str(gateway.get("accountType") or "UNKNOWN").upper()
        account_ok = system_mode != "AUTO_TRADE" or (
            account_type in {"DEMO", "REAL"}
            and verification_age is not None
            and float(verification_age) <= 5
            and (account_type != "REAL" or gateway.get("realAccountArmed") is True)
        )
        gateway_ok = bool(gateway.get("ok")) and gateway.get("configStale") is False
        # A real depth event timestamp is intentionally absent on supported
        # Matriks builds.  ``depthAgeSeconds=None`` therefore means
        # "timestamp unavailable", not a stale depth event.  Readiness must
        # not be permanently red solely for that contract limitation.  The
        # order-time preflight remains fail-closed: it independently requires
        # a reliable, valid order book for every BUY/SELL order.
        # Negatif yaş = zaman damgası gelecekte, yani saat/timezone kayması.
        # Emir yolu bunu zaten reddediyor (C# ValidateOrderMarketData ve
        # order_preflight._valid_age, ikisi de age >= 0 şartı arar), ama
        # readiness sadece "<= 15" baksaydı kaymayı sağlıklı gösterir ve
        # emirler sessizce "quote is stale" ile bloklanırken panel yeşil
        # kalırdı. Aynı fail-closed eşiği burada da uygula.
        quote_freshness_ok = bool(quote_ages) and all(0 <= age <= 15 for age in quote_ages)
        depth_event_freshness_available = bool(depth_ages)
        depth_freshness_ok = not depth_event_freshness_available or all(
            0 <= age <= 10 for age in depth_ages
        )
        freshness_ok = quote_freshness_ok and depth_freshness_ok
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
            "depthEventFreshnessAvailable": depth_event_freshness_available,
            "depthEventFreshnessRequiredForReadiness": False,
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
    }
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "checkedAt": datetime.now(timezone.utc).isoformat(),
            "checks": checks,
        },
    )
