"""Separate liveness and operational readiness endpoints."""

from __future__ import annotations

import asyncio
import math
import shutil
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings
from app.db.session import async_session_factory
from app.services.admin_config import get_system_mode, is_kill_switch_enabled
from app.services.daily_pnl import DailyLossGuardState, get_daily_loss_guard_status
from app.services.matriks_gateway import gateway_client
from app.services.scanner import scanner

router = APIRouter(tags=["Health"])
EXPECTED_MIGRATION = "20260720_15"


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
    runtime_system_mode: str | None = None
    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
            kill_switch = await is_kill_switch_enabled(session)
            runtime_system_mode = await get_system_mode(session)
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

    gateway_health: dict | None = None
    try:
        gateway = await gateway_client.health()
        gateway_health = gateway
        quote_age_by_symbol = gateway.get("quoteAgeSeconds") or {}
        configured_symbols = [str(symbol) for symbol in gateway.get("symbols") or []]
        quote_symbols = list(
            dict.fromkeys(configured_symbols or map(str, quote_age_by_symbol))
        )
        quote_ages: list[float] = []
        fresh_symbols: list[str] = []
        missing_symbols: list[str] = []
        stale_symbols: list[str] = []
        future_timestamp_symbols: list[str] = []
        for symbol in quote_symbols:
            raw_age = quote_age_by_symbol.get(symbol)
            if raw_age is None:
                missing_symbols.append(symbol)
                continue
            try:
                age = float(raw_age)
            except (TypeError, ValueError, OverflowError):
                stale_symbols.append(symbol)
                continue
            if not math.isfinite(age):
                stale_symbols.append(symbol)
                continue
            quote_ages.append(age)
            if age < 0:
                future_timestamp_symbols.append(symbol)
            elif age <= 15:
                fresh_symbols.append(symbol)
            else:
                stale_symbols.append(symbol)

        depth_ages: list[float] = []
        depth_age_invalid = False
        for raw_age in (gateway.get("depthAgeSeconds") or {}).values():
            if raw_age is None:
                continue
            try:
                age = float(raw_age)
            except (TypeError, ValueError, OverflowError):
                depth_age_invalid = True
                continue
            if not math.isfinite(age):
                depth_age_invalid = True
                continue
            depth_ages.append(age)
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
        # Readiness global veri akışını ölçer: en az bir configured sembolde
        # taze push event yeterlidir. Eksik/bayat/gelecek damgalı semboller
        # görünür biçimde raporlanır; order preflight her sembolü ayrıca ve
        # fail-closed doğrulamaya devam eder.
        quote_freshness_ok = bool(fresh_symbols)
        depth_event_freshness_available = bool(depth_ages) or depth_age_invalid
        depth_freshness_ok = (
            not depth_event_freshness_available
            or not depth_age_invalid
            and all(0 <= age <= 10 for age in depth_ages)
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
            "freshSymbolCount": len(fresh_symbols),
            "freshSymbols": fresh_symbols,
            "missingSymbols": missing_symbols,
            "staleSymbols": stale_symbols,
            "futureTimestampSymbols": future_timestamp_symbols,
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

    try:
        async with async_session_factory() as session:
            daily_loss = await asyncio.wait_for(
                get_daily_loss_guard_status(
                    session,
                    gateway_client,
                    gateway_health=(
                        gateway_health if gateway_health is not None else {}
                    ),
                ),
                timeout=5.0,
            )
        daily_loss_ok = daily_loss.status != DailyLossGuardState.UNAVAILABLE
        required = runtime_system_mode == "AUTO_TRADE" and daily_loss.enabled
        if required and not daily_loss_ok:
            ready = False
        checks["dailyLossGuard"] = {
            "ok": daily_loss_ok,
            "enabled": daily_loss.enabled,
            "status": daily_loss.status.value,
            "enforced": daily_loss.status == DailyLossGuardState.BREACHED,
            "blocksNewBuys": daily_loss.blocks_buy,
            "capitalSource": daily_loss.capital_source.value,
            "pnlAvailable": daily_loss.pnl is not None,
            "pnlComplete": (
                daily_loss.pnl is not None and not daily_loss.pnl.data_gaps
            ),
            "requiredForReadiness": required,
        }
    except Exception:
        if runtime_system_mode == "AUTO_TRADE":
            ready = False
        checks["dailyLossGuard"] = {
            "ok": False,
            "enabled": True,
            "status": DailyLossGuardState.UNAVAILABLE.value,
            "enforced": False,
            "blocksNewBuys": True,
            "capitalSource": "NONE",
            "pnlAvailable": False,
            "pnlComplete": False,
            "requiredForReadiness": runtime_system_mode == "AUTO_TRADE",
        }

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
