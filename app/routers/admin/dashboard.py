"""Admin dashboard, performance, self-check, and bot-status routes."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.db import (
    AccountNormalizationAudit,
    BotPosition,
    OrderLog,
    RiskDecision,
)
from app.services.admin_config import (
    get_system_mode,
    is_scanner_runtime_enabled,
)
from app.services.ai_provider import get_ai_provider_status
from app.services.block_reason_classifier import classify_block_reason
from app.services.cash_reservation import calculate_backend_reserved_cash
from app.services.daily_pnl import get_daily_loss_guard_status
from app.services.daily_trade_count import get_today_trade_counts
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    gateway_client,
)
from app.services.notifications import notification_service
from app.services.position_sync import position_synchronizer
from app.services.scanner import scanner
from app.services.performance_report import build_performance_report
from app.services.self_check import run_self_check
from app.services.trade_profile import (
    get_active_profile,
)

from app.routers.admin._shared import (
    admin_router,
    admin_api_router,
    templates,
    logger,
    require_admin,
    _split_csv_symbols,
    _config_lookup,
    _status_strip_context,
    _latest,
    _row_dict,
)


@admin_router.get("", response_class=HTMLResponse)
@admin_router.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    dashboard = await _dashboard_context()

    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "identity": identity,
            "active": "dashboard",
            **dashboard,
        },
    )


@admin_router.get("/performance", response_class=HTMLResponse)
async def admin_performance(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    range_value = str(request.query_params.get("range") or "7d")
    symbol = str(request.query_params.get("symbol") or "") or None
    try:
        report = await build_performance_report(range_value, symbol, gateway=gateway_client)
        error = None
    except Exception as exc:
        logger.warning("Performance report failed: %s", exc)
        report, error = (
            {
                "totalDecisions": 0,
                "allowedOrders": 0,
                "blockedDecisions": 0,
                "ordersSent": 0,
                "filledOrders": 0,
                "rejectedOrders": 0,
                "estimatedRealizedPnl": 0,
                "topBlockReason": "-",
                "latestDecisions": [],
            },
            str(exc),
        )
    return templates.TemplateResponse(
        request,
        "admin/performance.html",
        {
            "identity": identity,
            "active": "performance",
            "report": report,
            "error": error,
            "range": range_value,
            "symbol": symbol or "",
        },
    )


@admin_router.get("/self-check", response_class=HTMLResponse)
async def admin_self_check(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    return templates.TemplateResponse(
        request,
        "admin/self_check.html",
        {
            "identity": identity,
            "active": "self-check",
            "result": await run_self_check(),
        },
    )


@admin_router.post("/self-check/run", response_class=HTMLResponse)
async def admin_self_check_run(request: Request) -> HTMLResponse:
    return await admin_self_check(request)


@admin_api_router.get("/dashboard")
async def admin_api_dashboard(request: Request) -> dict[str, Any]:
    await require_admin(request)
    async with async_session_factory() as session:
        configs = await _config_lookup(session)
        today_counts = await get_today_trade_counts(session, "*")
        latest_risk = await _latest(session, RiskDecision, 20)
        latest_orders = await _latest(session, OrderLog, 20)

    return {
        "systemMode": configs["systemMode"].value,
        "killSwitchEnabled": configs["killSwitchEnabled"].value == "true",
        "todayTradeCount": today_counts.bot_count,
        "latestRiskDecisions": [_row_dict(row) for row in latest_risk],
        "latestOrderLogs": [_row_dict(row) for row in latest_orders],
    }


@admin_api_router.get("/bot-status")
async def admin_api_bot_status(request: Request) -> dict[str, Any]:
    """Return best-effort runtime status without making the admin API fragile."""
    await require_admin(request)
    return await _bot_status()


@admin_api_router.get("/performance")
async def admin_api_performance(request: Request) -> dict[str, Any]:
    await require_admin(request)
    return await build_performance_report(
        str(request.query_params.get("range") or "7d"),
        request.query_params.get("symbol"),
        gateway=gateway_client,
    )


@admin_api_router.get("/self-check")
@admin_api_router.post("/self-check/run")
async def admin_api_self_check(request: Request) -> dict[str, Any]:
    await require_admin(request)
    return await run_self_check()


@admin_api_router.post("/notifications/test")
async def admin_api_notification_test(request: Request) -> dict[str, str]:
    """Send a best-effort operational test without exposing Telegram secrets."""
    await require_admin(request)
    if not notification_service.enabled:
        return {"status": "disabled"}
    await notification_service.send(
        "info", "Trade AI test bildirimi", event_key="admin:test"
    )
    return {"status": "ok"}


async def _recent_block_reasons(session: Any, limit: int = 10) -> list[dict[str, Any]]:
    """Last N blocked (allow_order=False) risk decisions, most recent first."""
    stmt = (
        select(RiskDecision)
        .where(RiskDecision.allow_order.is_(False))
        .order_by(RiskDecision.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "symbol": row.symbol,
            "action": row.action,
            "reason": row.reason,
            "category": classify_block_reason(row.reason),
            "created_at": row.created_at,
        }
        for row in rows
    ]


async def _open_positions_pnl() -> dict[str, Any]:
    """Open bot_positions with a fresh-snapshot unrealized P&L per symbol.

    Best-effort: a single symbol's snapshot failing does not drop the others
    or the whole dashboard - it's shown with pnl=None instead.
    """
    try:
        async with async_session_factory() as session:
            rows = (
                (await session.execute(select(BotPosition).where(BotPosition.qty > 0)))
                .scalars()
                .all()
            )
    except Exception as exc:
        logger.warning("Open positions P&L query failed: %s", exc)
        return {"positions": [], "total_pnl": None, "error": str(exc)}

    positions: list[dict[str, Any]] = []
    total_pnl = 0.0
    total_known = 0
    for row in rows:
        current_price = None
        pnl = None
        try:
            snapshot = await gateway_client.get_snapshot(row.symbol)
            current_price = (snapshot.get("payload") or {}).get("lastPrice")
        except Exception:
            current_price = None
        if current_price is not None and row.avg_price is not None:
            pnl = (float(current_price) - float(row.avg_price)) * float(row.qty)
            total_pnl += pnl
            total_known += 1
        positions.append(
            {
                "symbol": row.symbol,
                "qty": row.qty,
                "avg_price": row.avg_price,
                "current_price": current_price,
                "pnl": pnl,
            }
        )
    return {
        "positions": positions,
        "total_pnl": total_pnl if total_known else None,
        "error": None,
    }


async def _dashboard_context() -> dict[str, Any]:
    """Load dashboard data defensively so operational visibility survives DB issues."""
    try:
        async with async_session_factory() as session:
            configs = await _config_lookup(session)
            active_profile = await get_active_profile(session)
            today_counts = await get_today_trade_counts(session, "*")
            latest_risk = await _latest(session, RiskDecision, 20)
            latest_orders = await _latest(session, OrderLog, 20)
            latest_account = await _latest(session, AccountNormalizationAudit, 1)
            recent_block_reasons = await _recent_block_reasons(session, 10)
            active_cash_reservation_tl = await calculate_backend_reserved_cash(session)
            status_ctx = await _status_strip_context(
                session, configs=configs, profile=active_profile
            )
        db_error = None
    except Exception as exc:
        logger.exception("Admin dashboard DB query failed")
        configs = {}
        active_profile = None
        today_trade_count = 0
        latest_risk = []
        latest_orders = []
        latest_account = []
        recent_block_reasons = []
        active_cash_reservation_tl = None
        status_ctx = {
            "status_mode": "UNKNOWN",
            "status_kill_switch": False,
            "status_profile_code": "UNKNOWN",
            "status_profile_risk_level": "UNKNOWN",
            "status_ai_degraded": None,
        }
        db_error = str(exc)
    else:
        today_trade_count = today_counts.bot_count
    return {
        "configs": configs,
        "active_profile": active_profile,
        "today_trade_count": today_trade_count,
        "today_order_limit": (
            active_profile.max_orders_per_day if active_profile else None
        ),
        "active_cash_reservation_tl": active_cash_reservation_tl,
        "recent_block_reasons": recent_block_reasons,
        "open_positions_pnl": await _open_positions_pnl(),
        "ai_provider_status": get_ai_provider_status(),
        "latest_risk": latest_risk,
        "latest_orders": latest_orders,
        "latest_account_normalization": latest_account[0] if latest_account else None,
        "bot_status": await _bot_status(db_error=db_error),
        "dashboard_db_error": db_error,
        **status_ctx,
    }


def _unavailable_daily_loss_summary(reason: str) -> dict[str, Any]:
    return {
        "status": "UNAVAILABLE",
        "enabled": True,
        "configuredPct": None,
        "configuredTl": None,
        "capitalSource": "NONE",
        "capitalBaseTl": None,
        "percentageCapTl": None,
        "effectiveCapTl": None,
        "pnl": None,
        "reason": reason,
    }


async def _bot_status(*, db_error: str | None = None) -> dict[str, Any]:
    """Collect gateway, scanner and runtime state; every source is optional."""
    result: dict[str, Any] = {
        "gateway": {"reachable": False, "health": None, "error": None},
        "scanner": scanner.get_status(),
        "positionSync": position_synchronizer.get_status(),
        "runtime": {"dbAvailable": db_error is None, "dbError": db_error},
    }
    # get_status() env varsayılanını gösterir; panel override'ı varsa onu yansıt.
    try:
        async with async_session_factory() as session:
            result["scanner"]["systemMode"] = await get_system_mode(session)
            result["scanner"]["panelEnabled"] = await is_scanner_runtime_enabled(
                session
            )
    except Exception:
        pass
    try:
        result["aiProvider"] = get_ai_provider_status()
    except Exception as exc:
        logger.warning("AI provider status query failed: %s", exc)
        result["aiProvider"] = {
            "providerName": None,
            "isDegraded": None,
            "consecutiveFailures": None,
            "error": str(exc),
        }
    gateway_health: dict[str, Any] | None = None
    try:
        health = await gateway_client.health()
        gateway_health = health
        result["gateway"] = {
            "reachable": True,
            "health": health,
            "positionsLoaded": health.get("positionsLoaded"),
            "subscriptionsInitialized": health.get("subscriptionsInitialized"),
            "requestCount": health.get("requestCount"),
            "symbols": health.get("symbols") or [],
            "quoteAgeSeconds": health.get("quoteAgeSeconds"),
            "orderLimits": health.get("orderLimits"),
            "profileCode": health.get("profileCode"),
            "mode": health.get("mode"),
            "error": None,
        }
    except (GatewayUnavailable, GatewayError) as exc:
        result["gateway"]["error"] = str(exc)
    except Exception as exc:
        logger.warning("Gateway status query failed: %s", exc)
        result["gateway"]["error"] = str(exc)

    if db_error is not None:
        result["runtime"]["dailyLossGuard"] = _unavailable_daily_loss_summary(
            "daily loss guard unavailable because database status is unavailable"
        )
        return result
    try:
        async with async_session_factory() as session:
            configs = await _config_lookup(session)
            profile = await get_active_profile(session)
            counts = await get_today_trade_counts(session, "*")
            latest_risk = await _latest(session, RiskDecision, 1)
            latest_order = await _latest(session, OrderLog, 1)
            latest_account = await _latest(session, AccountNormalizationAudit, 1)
        config_values = {key: item.value for key, item in configs.items()}
        config_hash = hashlib.sha256(
            json.dumps(config_values, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        result["runtime"].update(
            {
                "systemMode": config_values.get("systemMode", "OBSERVE_ONLY"),
                "realAccountArmed": config_values.get("realAccountArmed") == "true",
                "killSwitchEnabled": config_values.get("killSwitchEnabled") == "true",
                "activeTradeProfile": {
                    "code": profile.code,
                    "name": profile.name,
                    "riskLevel": profile.risk_level,
                },
                "todayTradeCount": counts.bot_count,
                "latestRiskDecision": _row_dict(latest_risk[0])
                if latest_risk
                else None,
                "latestOrderLog": _row_dict(latest_order[0]) if latest_order else None,
                "configHash": config_hash,
                "profileCode": profile.code,
                "symbolsCount": len(
                    _split_csv_symbols(config_values.get("allowedSymbols", ""))
                ),
            }
        )
        if latest_account:
            account = latest_account[0]
            result["runtime"]["accountNormalization"] = {
                "reliable": account.account_data_reliable,
                "ageSeconds": account.account_data_age_seconds,
                "brokerBuyingPowerTl": account.broker_reported_buying_power_tl,
                "backendReservedCashTl": account.backend_reserved_cash_tl,
                "effectiveAvailableCashTl": account.effective_available_cash_tl,
                "reservationHandling": account.reservation_handling,
                "marginBuyingEnabled": account.margin_buying_enabled,
                "unreliableReasons": account.unreliable_reasons,
            }
    except Exception as exc:
        logger.warning("Bot status DB query failed: %s", exc)
        result["runtime"].update({"dbAvailable": False, "dbError": str(exc)})

    if result["runtime"].get("dbAvailable"):
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
            result["runtime"]["dailyLossGuard"] = (
                daily_loss.authenticated_summary()
            )
        except asyncio.TimeoutError:
            logger.warning("Daily loss guard dashboard status timed out")
            result["runtime"]["dailyLossGuard"] = (
                _unavailable_daily_loss_summary(
                    "daily loss guard status timed out"
                )
            )
        except Exception as exc:
            logger.warning("Daily loss guard dashboard status failed: %s", exc)
            result["runtime"]["dailyLossGuard"] = (
                _unavailable_daily_loss_summary(
                    "daily loss guard status unavailable"
                )
            )
    else:
        result["runtime"]["dailyLossGuard"] = _unavailable_daily_loss_summary(
            "daily loss guard unavailable because database status is unavailable"
        )
    return result
