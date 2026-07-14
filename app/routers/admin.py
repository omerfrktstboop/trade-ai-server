"""Admin panel and admin API routes."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from decimal import Decimal
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from app.config import settings
from app.db.session import async_session_factory
from app.models.db import (
    AccountNormalizationAudit,
    AiDecision,
    BotPosition,
    KapEvent,
    ConfigAuditLog,
    LockedPosition,
    MarketSnapshot,
    ManualApprovalRequest,
    PositionManagementDecision,
    ResearchCandidate,
    ResearchCandidateEvent,
    TradeWatchlistSymbol,
    OrderLog,
    RiskDecision,
    TradeProfile,
)
from app.services.admin_config import (
    AdminConfigItem,
    RISKY_CONFIRMATION,
    build_admin_config_sections,
    get_admin_config_value,
    list_admin_configs,
    public_config_keys,
    set_admin_config_value,
    set_admin_config_values,
)
from app.services.block_reason_classifier import classify_block_reason
from app.services.daily_trade_count import get_today_trade_counts
from app.services.fundamentals_service import (
    NUMERIC_FIELDS as FUNDAMENTAL_NUMERIC_FIELDS,
    delete_fundamental,
    list_fundamentals,
    upsert_fundamental,
)
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    gateway_client,
)
from app.services.notifications import notification_service
from app.services.position_sync import position_synchronizer
from app.services.scanner import scanner
from app.services.replay import replay_batch
from app.services.performance_report import build_performance_report
from app.services.manual_approvals import approve_request, reject_request
from app.services.self_check import run_self_check
from app.services.signal_override import SELL_ALL_SENTINEL_QTY, create_override
from app.services.trade_profile import (
    EDITABLE_FIELDS,
    FIELD_TYPES,
    RISKY_CONFIRMATION as PROFILE_RISKY_CONFIRMATION,
    activate_profile,
    clone_profile,
    create_profile,
    delete_profile,
    disable_profile,
    get_active_profile,
    get_profile,
    list_profiles,
    update_profile,
)
from app.services.research_pipeline import add_manual_trade_symbol

admin_router = APIRouter(tags=["Admin"])
admin_api_router = APIRouter(tags=["Admin API"])

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

_DISPLAY_TZ = ZoneInfo("Europe/Istanbul")


async def _notify_gateway_config_reload() -> None:
    """Best-effort push; gateway also polls every 60 seconds."""
    try:
        response = await gateway_client.reload_config()
        logger.info(
            "Gateway config reload notified profile=%s ok=%s symbols=%s",
            response.get("profileCode"),
            response.get("ok"),
            ",".join(response.get("symbols") or []),
        )
    except (GatewayUnavailable, GatewayError) as exc:
        logger.warning("Gateway config reload notification failed: %s", exc)


def _local_time(value: datetime | None) -> str:
    """Render a DB timestamp (stored as UTC via func.now()) in Europe/Istanbul
    local time for the admin panel — DB values stay UTC, only the display
    layer converts, so sorting/comparisons elsewhere are unaffected."""
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(_DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")


templates.env.filters["local_time"] = _local_time

logger = logging.getLogger(__name__)

ADMIN_COOKIE_NAME = "trade_ai_admin"
ADMIN_COOKIE_TTL_SECONDS = 8 * 60 * 60

# Log tables deletable from /admin/logs, keyed by URL slug.
LOG_TABLES: dict[str, Any] = {
    "ai-decisions": AiDecision,
    "risk-decisions": RiskDecision,
    "order-logs": OrderLog,
    "audit-logs": ConfigAuditLog,
}


def _split_csv_symbols(raw: str) -> set[str]:
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


class AdminConfigUpdate(BaseModel):
    value: Any
    reason: str | None = None
    confirmation: str | None = None


class AdminConfigBatchUpdate(BaseModel):
    values: dict[str, Any]
    reason: str | None = None
    confirmation: str | None = None


class EmergencyAction(BaseModel):
    reason: str | None = None
    confirmation: str | None = None


class TradeProfileFieldsBody(BaseModel):
    name: str | None = None
    description: str | None = None
    risk_level: str | None = Field(None, alias="riskLevel")
    allowed_modes: str | None = Field(None, alias="allowedModes")
    max_order_value_tl: Decimal | None = Field(None, alias="maxOrderValueTl")
    max_qty_per_order: int | None = Field(None, alias="maxQtyPerOrder")
    max_position_value_per_symbol: Decimal | None = Field(
        None, alias="maxPositionValuePerSymbol"
    )
    risk_per_trade_pct: Decimal | None = Field(None, alias="riskPerTradePct")
    max_cash_utilization_pct: Decimal | None = Field(
        None, alias="maxCashUtilizationPct"
    )
    max_account_exposure_pct: Decimal | None = Field(
        None, alias="maxAccountExposurePct"
    )
    min_order_value_tl: Decimal | None = Field(None, alias="minOrderValueTl")
    min_stop_distance_pct: Decimal | None = Field(None, alias="minStopDistancePct")
    max_stop_distance_pct: Decimal | None = Field(None, alias="maxStopDistancePct")
    minimum_stop_slippage_pct: Decimal | None = Field(
        None, alias="minimumStopSlippagePct"
    )
    maximum_stop_slippage_pct: Decimal | None = Field(
        None, alias="maximumStopSlippagePct"
    )
    profile_stop_slippage_pct: Decimal | None = Field(
        None, alias="profileStopSlippagePct"
    )
    max_account_data_age_seconds: Decimal | None = Field(
        None, alias="maxAccountDataAgeSeconds"
    )
    max_orders_per_day: int | None = Field(None, alias="maxOrdersPerDay")
    max_orders_per_symbol_per_day: int | None = Field(
        None, alias="maxOrdersPerSymbolPerDay"
    )
    min_confidence_for_buy: float | None = Field(None, alias="minConfidenceForBuy")
    min_confidence_for_sell: float | None = Field(None, alias="minConfidenceForSell")
    max_natr_for_buy: float | None = Field(None, alias="maxNatrForBuy")
    max_depth_queue_drop_pct_for_buy: float | None = Field(
        None, alias="maxDepthQueueDropPctForBuy"
    )
    max_spread_pct_for_buy: float | None = Field(None, alias="maxSpreadPctForBuy")
    min_depth_bid_ask_ratio_top10_for_buy: float | None = Field(
        None, alias="minDepthBidAskRatioTop10ForBuy"
    )
    max_depth_sell_pressure_score_for_buy: float | None = Field(
        None, alias="maxDepthSellPressureScoreForBuy"
    )
    block_buy_on_strong_sell_pressure: bool | None = Field(
        None, alias="blockBuyOnStrongSellPressure"
    )
    block_buy_on_near_ask_wall: bool | None = Field(None, alias="blockBuyOnNearAskWall")
    near_wall_distance_pct: float | None = Field(None, alias="nearWallDistancePct")
    require_alpha_trend_alignment: bool | None = Field(
        None, alias="requireAlphaTrendAlignment"
    )
    require_indicator_consensus_alignment: bool | None = Field(
        None, alias="requireIndicatorConsensusAlignment"
    )
    allow_sell_long_term: bool | None = Field(None, alias="allowSellLongTerm")
    allow_short_selling: bool | None = Field(None, alias="allowShortSelling")
    allow_real_live: bool | None = Field(None, alias="allowRealLive")
    allow_demo_live: bool | None = Field(None, alias="allowDemoLive")
    allow_margin_buying: bool | None = Field(None, alias="allowMarginBuying")
    scan_interval_minutes: int | None = Field(None, alias="scanIntervalMinutes")
    max_fetch_loop_per_session: int | None = Field(None, alias="maxFetchLoopPerSession")
    order_time_in_force: str | None = Field(None, alias="orderTimeInForce")
    indicator_period: str | None = Field(None, alias="indicatorPeriod")

    model_config = {"populate_by_name": True}


class TradeProfileCreateBody(TradeProfileFieldsBody):
    code: str


class TradeProfileUpdateBody(TradeProfileFieldsBody):
    reason: str | None = None
    confirmation: str | None = None


class TradeProfileActivateBody(BaseModel):
    reason: str | None = None
    confirmation: str | None = None


class TradeProfileCloneBody(BaseModel):
    new_code: str = Field(alias="newCode")
    new_name: str = Field(alias="newName")

    model_config = {"populate_by_name": True}


class FundamentalBody(BaseModel):
    period: str
    fcf_growth_pct: float | None = Field(None, alias="fcfGrowthPct")
    debt_to_equity: float | None = Field(None, alias="debtToEquity")
    net_margin_pct: float | None = Field(None, alias="netMarginPct")
    net_margin_change_pt: float | None = Field(None, alias="netMarginChangePt")
    revenue_growth_pct: float | None = Field(None, alias="revenueGrowthPct")
    notes: str | None = None

    model_config = {"populate_by_name": True}


async def _admin_identity(request: Request) -> str | None:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        if hmac.compare_digest(token, settings.effective_admin_api_token):
            return "api-token"

    cookie = request.cookies.get(ADMIN_COOKIE_NAME)
    if cookie and _verify_admin_cookie(cookie):
        return "admin"

    return None


async def require_admin(request: Request) -> str:
    identity = await _admin_identity(request)
    if identity:
        return identity
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Admin authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


@admin_router.get("/login", response_class=HTMLResponse)
async def admin_login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin/login.html",
        {"error": None},
    )


@admin_router.post("/login")
async def admin_login(request: Request) -> Any:
    form = await request.form()
    password = str(form.get("password") or "")
    if not hmac.compare_digest(password, settings.admin_password):
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {"error": "Invalid admin password"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    response = RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        _make_admin_cookie(),
        max_age=ADMIN_COOKIE_TTL_SECONDS,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
    )
    return response


@admin_router.post("/logout")
async def admin_logout() -> RedirectResponse:
    response = RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(ADMIN_COOKIE_NAME)
    return response


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
        report = await build_performance_report(range_value, symbol)
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


@admin_router.get("/approvals", response_class=HTMLResponse)
async def admin_approvals(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    async with async_session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(ManualApprovalRequest).order_by(
                        ManualApprovalRequest.created_at.desc()
                    )
                )
            )
            .scalars()
            .all()
        )
    return templates.TemplateResponse(
        request,
        "admin/approvals.html",
        {"identity": identity, "active": "approvals", "rows": rows},
    )


@admin_router.get("/positions/management", response_class=HTMLResponse)
async def admin_position_management(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    async with async_session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(PositionManagementDecision)
                    .order_by(PositionManagementDecision.created_at.desc())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
    return templates.TemplateResponse(
        request,
        "admin/position_management.html",
        {"identity": identity, "active": "positions", "rows": rows},
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


@admin_router.get("/watchlist", response_class=HTMLResponse)
async def admin_watchlist(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    async with async_session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(TradeWatchlistSymbol, ResearchCandidate).outerjoin(
                        ResearchCandidate,
                        ResearchCandidate.symbol == TradeWatchlistSymbol.symbol,
                    )
                )
            ).all()
        )
    return templates.TemplateResponse(
        request,
        "admin/watchlist.html",
        {"identity": identity, "active": "watchlist", "rows": rows},
    )


@admin_router.post("/self-check/run", response_class=HTMLResponse)
async def admin_self_check_run(request: Request) -> HTMLResponse:
    return await admin_self_check(request)


@admin_router.post("/approvals/{approval_id}/approve")
async def admin_approve(request: Request, approval_id: int) -> RedirectResponse:
    identity = await require_admin(request)
    form = await request.form()
    await approve_request(approval_id, identity, str(form.get("admin_note") or ""))
    return RedirectResponse("/admin/approvals", status_code=303)


@admin_router.post("/approvals/{approval_id}/reject")
async def admin_reject(request: Request, approval_id: int) -> RedirectResponse:
    identity = await require_admin(request)
    form = await request.form()
    await reject_request(approval_id, identity, str(form.get("admin_note") or ""))
    return RedirectResponse("/admin/approvals", status_code=303)


@admin_router.get("/why-blocked", response_class=HTMLResponse)
async def admin_why_blocked(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    symbol = str(request.query_params.get("symbol") or "").upper()
    category = str(request.query_params.get("category") or "").upper()
    action = str(request.query_params.get("action") or "").upper()
    only_blocked = request.query_params.get("only_blocked") == "1"
    rows: list[dict[str, Any]] = []
    try:
        async with async_session_factory() as session:
            risks = await _latest(session, RiskDecision, 250)
            orders = await _latest(session, OrderLog, 250)
            status_ctx = await _status_strip_context(session)
        for row in risks:
            if only_blocked and row.allow_order:
                continue
            reason = row.reason or ""
            rows.append(
                {
                    "created_at": row.created_at,
                    "request_id": row.request_id,
                    "symbol": row.symbol,
                    "action": row.action,
                    "confidence": row.confidence,
                    "risk_score": row.risk_score,
                    "allow_order": row.allow_order,
                    "order_type": row.order_type,
                    "qty": row.qty,
                    "price": row.entry_max,
                    "reason": reason,
                    "category": classify_block_reason(reason),
                }
            )
        for row in orders:
            if row.status.upper() not in {"REJECTED", "ERROR", "CANCELED"}:
                continue
            reason = row.matrix_message or row.status
            rows.append(
                {
                    "created_at": row.created_at,
                    "request_id": row.request_id,
                    "symbol": row.symbol,
                    "action": row.action,
                    "confidence": None,
                    "risk_score": None,
                    "allow_order": False,
                    "order_type": "LIMIT",
                    "qty": row.qty,
                    "price": row.price,
                    "reason": reason,
                    "category": classify_block_reason(reason),
                }
            )
    except Exception as exc:
        logger.warning("Why blocked query failed: %s", exc)
        status_ctx = {
            "status_mode": "UNKNOWN",
            "status_kill_switch": False,
            "status_profile_code": "UNKNOWN",
            "status_profile_risk_level": "UNKNOWN",
        }
    rows = [
        r
        for r in rows
        if (not symbol or r["symbol"] == symbol)
        and (not category or r["category"] == category)
        and (not action or r["action"] == action)
    ]
    rows.sort(key=lambda r: r["created_at"] or datetime.min, reverse=True)
    categories = Counter(r["category"] for r in rows)
    symbols = Counter(r["symbol"] for r in rows)
    summary = {
        "total": len(rows),
        "category": categories.most_common(1)[0][0] if categories else "-",
        "symbol": symbols.most_common(1)[0][0] if symbols else "-",
        "confidence_low": categories.get("CONFIDENCE_LOW", 0),
    }
    return templates.TemplateResponse(
        request,
        "admin/why_blocked.html",
        {
            "identity": identity,
            "active": "why-blocked",
            "rows": rows,
            "summary": summary,
            "filters": {
                "symbol": symbol,
                "category": category,
                "action": action,
                "only_blocked": only_blocked,
            },
            **status_ctx,
        },
    )


@admin_router.get("/replay", response_class=HTMLResponse)
async def admin_replay(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    profiles, status_ctx, error = await _replay_page_context()
    return templates.TemplateResponse(
        request,
        "admin/replay.html",
        {
            "identity": identity,
            "active": "replay",
            "profiles": profiles,
            "result": None,
            "error": error,
            **status_ctx,
        },
    )


@admin_router.post("/replay/run", response_class=HTMLResponse)
async def admin_replay_run(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    form = await request.form()
    profile_code = str(form.get("profile_code") or "") or None
    mode = str(form.get("mode") or "PAPER")
    symbols = [
        s.strip().upper()
        for s in str(form.get("symbols") or "").split(",")
        if s.strip()
    ]
    limit = min(200, max(1, int(form.get("limit") or 100)))
    try:
        result = await replay_batch(
            profile_code=profile_code, symbols=symbols or None, limit=limit, mode=mode
        )
        error = None
    except Exception as exc:
        logger.exception("Replay run failed")
        result = None
        error = f"Replay unavailable: {exc}"
    profiles, status_ctx, context_error = await _replay_page_context()
    return templates.TemplateResponse(
        request,
        "admin/replay.html",
        {
            "identity": identity,
            "active": "replay",
            "profiles": profiles,
            "result": result,
            "error": error or context_error,
            **status_ctx,
        },
    )


@admin_router.get("/config", response_class=HTMLResponse)
async def admin_config_page(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    async with async_session_factory() as session:
        configs = await _config_lookup(session)
        config_sections = build_admin_config_sections(configs.values())
        status_ctx = await _status_strip_context(session, configs=configs)

    return templates.TemplateResponse(
        request,
        "admin/config.html",
        {
            "identity": identity,
            "active": "config",
            "configs": configs,
            "config_sections": config_sections,
            "confirmation": RISKY_CONFIRMATION,
            "error": None,
            "message": None,
            "form_values": {},
            "reason": "",
            **status_ctx,
        },
    )


@admin_router.post("/config", response_class=HTMLResponse)
async def admin_config_update(request: Request) -> Any:
    identity = await require_admin(request)
    form = await request.form()
    reason = str(form.get("reason") or "Admin panel update")
    confirmation = str(form.get("confirmation") or "")

    try:
        async with async_session_factory() as session:
            values = {
                key: form[key] for key in public_config_keys() if key in form
            }
            await set_admin_config_values(
                session,
                values,
                changed_by=identity,
                reason=reason,
                confirmation=confirmation,
            )
    except ValueError as exc:
        async with async_session_factory() as session:
            configs = await _config_lookup(session)
            config_sections = build_admin_config_sections(configs.values())
            status_ctx = await _status_strip_context(session, configs=configs)
        return templates.TemplateResponse(
            request,
            "admin/config.html",
            {
                "identity": identity,
                "active": "config",
                "configs": configs,
                "config_sections": config_sections,
                "confirmation": RISKY_CONFIRMATION,
                "error": str(exc),
                "message": None,
                "form_values": form,
                "reason": reason,
                **status_ctx,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    await _notify_gateway_config_reload()
    return RedirectResponse("/admin/config", status_code=status.HTTP_303_SEE_OTHER)


async def _gateway_positions_context() -> dict[str, Any]:
    try:
        payload = await gateway_client.get_positions()
        positions = payload.get("positions")
        if not isinstance(positions, list):
            positions = []
        return {
            "gateway_positions": positions,
            "gateway_positions_meta": {
                "positions_loaded": payload.get("positionsLoaded"),
                "snapshot_complete": payload.get("snapshotCompleteFlag"),
                "snapshot_non_empty": payload.get("snapshotNonEmpty"),
                "snapshot_age_seconds": payload.get("snapshotAgeSeconds"),
                "snapshot_generation": payload.get("snapshotGeneration"),
                "confidence": payload.get("confidence"),
            },
            "gateway_positions_error": None,
        }
    except (GatewayUnavailable, GatewayError, ValueError, TypeError) as exc:
        return {
            "gateway_positions": [],
            "gateway_positions_meta": {},
            "gateway_positions_error": str(exc),
        }


@admin_router.get("/positions", response_class=HTMLResponse)
async def admin_positions(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    async with async_session_factory() as session:
        bot_positions = await _latest(
            session, BotPosition, 100, order_field="updated_at"
        )
        locked_positions = await _latest(
            session, LockedPosition, 100, order_field="created_at"
        )
        allowed_raw = await get_admin_config_value(session, "allowedSymbols")
        status_ctx = await _status_strip_context(session)

    allowed_symbols = _split_csv_symbols(allowed_raw)
    gateway_ctx = await _gateway_positions_context()

    return templates.TemplateResponse(
        request,
        "admin/positions.html",
        {
            "identity": identity,
            "active": "positions",
            "bot_positions": bot_positions,
            "locked_positions": locked_positions,
            "allowed_symbols": allowed_symbols,
            "confirmation": RISKY_CONFIRMATION,
            "error": None,
            "message": None,
            **gateway_ctx,
            **status_ctx,
        },
    )


async def _positions_page_error(
    request: Request, identity: str, error: str
) -> HTMLResponse:
    async with async_session_factory() as session:
        bot_positions = await _latest(
            session, BotPosition, 100, order_field="updated_at"
        )
        locked_positions = await _latest(
            session, LockedPosition, 100, order_field="created_at"
        )
        allowed_raw = await get_admin_config_value(session, "allowedSymbols")
        status_ctx = await _status_strip_context(session)
    gateway_ctx = await _gateway_positions_context()

    return templates.TemplateResponse(
        request,
        "admin/positions.html",
        {
            "identity": identity,
            "active": "positions",
            "bot_positions": bot_positions,
            "locked_positions": locked_positions,
            "allowed_symbols": _split_csv_symbols(allowed_raw),
            "confirmation": RISKY_CONFIRMATION,
            "error": error,
            "message": None,
            **gateway_ctx,
            **status_ctx,
        },
        status_code=status.HTTP_400_BAD_REQUEST,
    )


@admin_router.post("/positions/{symbol}/force-override")
async def admin_force_override(request: Request, symbol: str) -> Any:
    identity = await require_admin(request)
    form = await request.form()
    action = str(form.get("action") or "").strip().upper()
    reason = str(form.get("reason") or "Manual test override")
    confirmation = str(form.get("confirmation") or "")

    if confirmation != RISKY_CONFIRMATION:
        return await _positions_page_error(
            request,
            identity,
            f"force-override requires confirmation={RISKY_CONFIRMATION}",
        )
    if action not in ("BUY", "SELL"):
        return await _positions_page_error(
            request, identity, f"Unsupported override action: {action or '(empty)'}"
        )

    qty = SELL_ALL_SENTINEL_QTY if action == "SELL" else _to_float(form.get("qty"))
    async with async_session_factory() as session:
        await create_override(
            session,
            symbol,
            action,
            qty,
            reason=reason,
            created_by=identity,
            entry_min=_to_float(form.get("entryMin")),
            entry_max=_to_float(form.get("entryMax")),
            stop_loss=_to_float(form.get("stopLoss")),
            target_price=_to_float(form.get("targetPrice")),
        )

    return RedirectResponse("/admin/positions", status_code=status.HTTP_303_SEE_OTHER)


@admin_router.post("/positions/force-sell-all")
async def admin_force_sell_all(request: Request) -> Any:
    identity = await require_admin(request)
    form = await request.form()
    reason = str(form.get("reason") or "Force-sell-all test")
    confirmation = str(form.get("confirmation") or "")

    if confirmation != RISKY_CONFIRMATION:
        return await _positions_page_error(
            request,
            identity,
            f"force-sell-all requires confirmation={RISKY_CONFIRMATION}",
        )

    async with async_session_factory() as session:
        positions = await _latest(session, BotPosition, 500, order_field="updated_at")
        symbols = [p.symbol for p in positions if p.qty and p.qty != 0]
        for symbol in symbols:
            await create_override(
                session,
                symbol,
                "SELL",
                SELL_ALL_SENTINEL_QTY,
                reason=reason,
                created_by=identity,
            )

    return RedirectResponse("/admin/positions", status_code=status.HTTP_303_SEE_OTHER)


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@admin_router.post("/positions/add-to-watchlist")
async def admin_add_to_watchlist(request: Request) -> RedirectResponse:
    identity = await require_admin(request)
    form = await request.form()
    symbol = str(form.get("symbol") or "").strip().upper()

    if symbol:
        async with async_session_factory() as session:
            current = _split_csv_symbols(
                await get_admin_config_value(session, "allowedSymbols")
            )
            current.add(symbol)
            await set_admin_config_value(
                session,
                "allowedSymbols",
                ",".join(sorted(current)),
                changed_by=identity,
                reason=f"Added {symbol} to watchlist from Positions page",
            )
            await add_manual_trade_symbol(
                session,
                symbol,
                reason=f"Explicit admin override by {identity} from Positions page",
            )
            await session.commit()

        await _notify_gateway_config_reload()

    return RedirectResponse("/admin/positions", status_code=status.HTTP_303_SEE_OTHER)


# ── Trade Profiles ───────────────────────────────────────────────────────────


def _parse_profile_form_fields(form: Any) -> dict[str, Any]:
    """Extract EDITABLE_FIELDS present in an HTML form, cast per FIELD_TYPES.

    Bool fields are rendered as <select>true/false</select> in the template
    (not checkboxes) so they're always present and unambiguous for both
    create (full form) and update (may omit unchanged fields).
    """
    changes: dict[str, Any] = {}
    for field in EDITABLE_FIELDS:
        raw = form.get(field)
        if raw is None or raw == "":
            continue
        field_type = FIELD_TYPES[field]
        if field_type is bool:
            changes[field] = str(raw).strip().lower() in ("true", "1", "yes", "on")
        elif field_type is float:
            value = _to_float(raw)
            if value is not None:
                changes[field] = value
        elif field_type is Decimal:
            changes[field] = Decimal(str(raw))
        elif field_type is int:
            value = _to_float(raw)
            if value is not None:
                changes[field] = int(value)
        else:
            changes[field] = str(raw).strip()
    return changes


async def _trade_profiles_page(
    request: Request,
    identity: str,
    *,
    error: str | None = None,
    message: str | None = None,
) -> HTMLResponse:
    async with async_session_factory() as session:
        profiles = await list_profiles(session)
        active = await get_active_profile(session)
        status_ctx = await _status_strip_context(session, profile=active)

    return templates.TemplateResponse(
        request,
        "admin/trade_profiles.html",
        {
            "identity": identity,
            "active": "trade-profiles",
            "profiles": profiles,
            "active_code": active.code,
            "active_profile": active,
            "confirmation": PROFILE_RISKY_CONFIRMATION,
            "error": error,
            "message": message,
            **status_ctx,
        },
    )


@admin_router.get("/trade-profiles", response_class=HTMLResponse)
async def admin_trade_profiles(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    return await _trade_profiles_page(request, identity)


@admin_router.post("/trade-profiles/create")
async def admin_trade_profiles_create(request: Request) -> Any:
    identity = await require_admin(request)
    form = await request.form()
    code = str(form.get("code") or "").strip().upper()
    name = str(form.get("name") or "").strip()
    description = str(form.get("description") or "")
    risk_level = str(form.get("risk_level") or "MEDIUM").strip().upper()
    changes = _parse_profile_form_fields(form)
    for field in ("name", "description", "risk_level"):
        changes.pop(field, None)

    try:
        async with async_session_factory() as session:
            await create_profile(
                session,
                code=code,
                name=name,
                description=description,
                risk_level=risk_level,
                changed_by=identity,
                **changes,
            )
    except (ValueError, TypeError) as exc:
        return await _trade_profiles_page(request, identity, error=str(exc))

    await _notify_gateway_config_reload()
    return RedirectResponse(
        "/admin/trade-profiles", status_code=status.HTTP_303_SEE_OTHER
    )


@admin_router.post("/trade-profiles/{code}/update")
async def admin_trade_profiles_update(request: Request, code: str) -> Any:
    identity = await require_admin(request)
    form = await request.form()
    reason = str(form.get("reason") or "Trade profile update")
    confirmation = str(form.get("confirmation") or "")
    changes = _parse_profile_form_fields(form)

    try:
        async with async_session_factory() as session:
            await update_profile(
                session,
                code,
                changes,
                changed_by=identity,
                reason=reason,
                confirmation=confirmation,
            )
    except ValueError as exc:
        return await _trade_profiles_page(request, identity, error=str(exc))

    await _notify_gateway_config_reload()
    return RedirectResponse(
        "/admin/trade-profiles", status_code=status.HTTP_303_SEE_OTHER
    )


@admin_router.post("/trade-profiles/{code}/activate")
async def admin_trade_profiles_activate(request: Request, code: str) -> Any:
    identity = await require_admin(request)
    form = await request.form()
    reason = str(form.get("reason") or f"Activated {code}")
    confirmation = str(form.get("confirmation") or "")

    try:
        async with async_session_factory() as session:
            await activate_profile(
                session,
                code,
                changed_by=identity,
                reason=reason,
                confirmation=confirmation,
            )
    except ValueError as exc:
        return await _trade_profiles_page(request, identity, error=str(exc))

    await _notify_gateway_config_reload()
    return RedirectResponse(
        "/admin/trade-profiles", status_code=status.HTTP_303_SEE_OTHER
    )


@admin_router.post("/trade-profiles/{code}/clone")
async def admin_trade_profiles_clone(request: Request, code: str) -> Any:
    identity = await require_admin(request)
    form = await request.form()
    new_code = str(form.get("new_code") or "").strip().upper()
    new_name = str(form.get("new_name") or "").strip()

    try:
        async with async_session_factory() as session:
            await clone_profile(
                session, code, new_code=new_code, new_name=new_name, changed_by=identity
            )
    except ValueError as exc:
        return await _trade_profiles_page(request, identity, error=str(exc))

    return RedirectResponse(
        "/admin/trade-profiles", status_code=status.HTTP_303_SEE_OTHER
    )


@admin_router.post("/trade-profiles/{code}/disable")
async def admin_trade_profiles_disable(request: Request, code: str) -> Any:
    identity = await require_admin(request)
    try:
        async with async_session_factory() as session:
            await disable_profile(session, code, changed_by=identity)
    except ValueError as exc:
        return await _trade_profiles_page(request, identity, error=str(exc))

    return RedirectResponse(
        "/admin/trade-profiles", status_code=status.HTTP_303_SEE_OTHER
    )


@admin_router.post("/trade-profiles/{code}/delete")
async def admin_trade_profiles_delete(request: Request, code: str) -> Any:
    identity = await require_admin(request)
    try:
        async with async_session_factory() as session:
            await delete_profile(session, code, changed_by=identity)
    except ValueError as exc:
        return await _trade_profiles_page(request, identity, error=str(exc))

    return RedirectResponse(
        "/admin/trade-profiles", status_code=status.HTTP_303_SEE_OTHER
    )


# ── Fundamentals (admin-entered quarterly balance-sheet data) ────────────────


async def _fundamentals_page(
    request: Request, identity: str, *, error: str | None = None
) -> HTMLResponse:
    async with async_session_factory() as session:
        rows = await list_fundamentals(session)
        allowed_raw = await get_admin_config_value(session, "allowedSymbols")
        status_ctx = await _status_strip_context(session)

    rows_by_symbol = {row.symbol: row for row in rows}
    # Watchlist symbols first (alphabetical), then any leftover rows for
    # symbols that have since been removed from the watchlist.
    symbols = sorted(_split_csv_symbols(allowed_raw))
    extra_symbols = sorted(set(rows_by_symbol) - set(symbols))

    return templates.TemplateResponse(
        request,
        "admin/fundamentals.html",
        {
            "identity": identity,
            "active": "fundamentals",
            "symbols": symbols,
            "extra_symbols": extra_symbols,
            "rows_by_symbol": rows_by_symbol,
            "error": error,
            **status_ctx,
        },
    )


@admin_router.get("/fundamentals", response_class=HTMLResponse)
async def admin_fundamentals(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    return await _fundamentals_page(request, identity)


@admin_router.get("/kap", response_class=HTMLResponse)
async def admin_kap(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    async with async_session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(KapEvent).order_by(KapEvent.cached_at.desc()).limit(200)
                )
            )
            .scalars()
            .all()
        )
    return templates.TemplateResponse(
        request,
        "admin/kap.html",
        {"identity": identity, "active": "kap", "rows": rows, "risk_only": False},
    )


@admin_router.get("/kap-risk", response_class=HTMLResponse)
async def admin_kap_risk(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    async with async_session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(KapEvent)
                    .where(KapEvent.risk_level.in_(("HIGH", "BLOCKING")))
                    .order_by(KapEvent.cached_at.desc())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
    return templates.TemplateResponse(
        request,
        "admin/kap.html",
        {"identity": identity, "active": "kap", "rows": rows, "risk_only": True},
    )


@admin_router.post("/fundamentals/{symbol}")
async def admin_fundamentals_upsert(request: Request, symbol: str) -> Any:
    identity = await require_admin(request)
    form = await request.form()
    numeric = {
        field: _to_float(form.get(field)) for field in FUNDAMENTAL_NUMERIC_FIELDS
    }

    try:
        async with async_session_factory() as session:
            await upsert_fundamental(
                session,
                symbol,
                period=str(form.get("period") or ""),
                changed_by=identity,
                notes=str(form.get("notes") or "").strip() or None,
                **numeric,
            )
    except ValueError as exc:
        return await _fundamentals_page(request, identity, error=str(exc))

    return RedirectResponse(
        "/admin/fundamentals", status_code=status.HTTP_303_SEE_OTHER
    )


@admin_router.post("/fundamentals/{symbol}/delete")
async def admin_fundamentals_delete(request: Request, symbol: str) -> Any:
    await require_admin(request)
    async with async_session_factory() as session:
        await delete_fundamental(session, symbol)
    return RedirectResponse(
        "/admin/fundamentals", status_code=status.HTTP_303_SEE_OTHER
    )


# ── Research report ("Fırsat Sıralaması") ────────────────────────────────────

# Decisions older than this aren't ranked — the market has moved on.
RESEARCH_FRESH_WINDOW = timedelta(hours=24)


def _research_rr_ratio(
    entry_max: float | None, stop_loss: float | None, target_price: float | None
) -> float | None:
    """Reward/risk ratio: (target - entry) / (entry - stop).

    This is the "asymmetric opportunity" measure — how many units of upside
    per unit of downside. None when any leg is missing or the stop isn't
    below the entry (degenerate/invalid geometry).
    """
    if entry_max is None or stop_loss is None or target_price is None:
        return None
    risk = entry_max - stop_loss
    if risk <= 0:
        return None
    return (target_price - entry_max) / risk


def _research_sort_key(row: dict[str, Any]) -> tuple:
    """BUYs first (best R/R, then confidence), then WAITs by confidence,
    then SELLs. Rows with an R/R ratio outrank same-action rows without."""
    priority = {"BUY": 0, "WAIT": 1, "SELL": 2}.get(row["action"], 3)
    rr = row["rr"]
    return (
        priority,
        0 if rr is not None else 1,
        -(rr if rr is not None else 0.0),
        -(row["confidence"] or 0.0),
    )


def _research_rank_rows(decisions: list[Any]) -> list[dict[str, Any]]:
    """Turn latest-per-symbol RiskDecision rows into a ranked opportunity
    list. Pure function so the ranking logic is unit-testable."""
    rows: list[dict[str, Any]] = []
    for d in decisions:
        rows.append(
            {
                "symbol": d.symbol,
                "action": d.action,
                "confidence": d.confidence,
                "risk_score": d.risk_score,
                "rr": _research_rr_ratio(d.entry_max, d.stop_loss, d.target_price),
                "entry_min": d.entry_min,
                "entry_max": d.entry_max,
                "stop_loss": d.stop_loss,
                "target_price": d.target_price,
                "reason": d.reason,
                "request_id": d.request_id,
                "created_at": d.created_at,
            }
        )
    rows.sort(key=_research_sort_key)
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


@admin_router.get("/research", response_class=HTMLResponse)
async def admin_research(request: Request) -> HTMLResponse:
    """Show discovery candidates, research scores, promotion state and timeline."""
    identity = await require_admin(request)
    selected_filter = str(request.query_params.get("filter") or "all").lower()
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        candidates = (
            (
                await session.execute(
                    select(ResearchCandidate).order_by(
                        ResearchCandidate.last_detected_at.desc()
                    )
                )
            )
            .scalars()
            .all()
        )
        events = (
            (
                await session.execute(
                    select(ResearchCandidateEvent)
                    .order_by(ResearchCandidateEvent.created_at.desc())
                    .limit(1000)
                )
            )
            .scalars()
            .all()
        )
        active_trade_rows = (
            (
                await session.execute(
                    select(TradeWatchlistSymbol).where(
                        TradeWatchlistSymbol.is_active.is_(True),
                        (TradeWatchlistSymbol.expires_at.is_(None))
                        | (TradeWatchlistSymbol.expires_at >= now),
                    )
                )
            )
            .scalars()
            .all()
        )
        trade_by_symbol = {row.symbol: row for row in active_trade_rows}
        trade_symbols = set(trade_by_symbol)
        status_ctx = await _status_strip_context(session)

    def visible(row: ResearchCandidate) -> bool:
        if selected_filter == "pending":
            return row.status in {"DETECTED", "RESEARCH_PENDING"}
        if selected_filter == "near":
            return 60 <= float(row.ai_research_score or 0) < 75
        if selected_filter == "promoted":
            return row.status == "PROMOTED" or row.symbol in trade_symbols
        if selected_filter == "rejected":
            return row.status == "REJECTED"
        if selected_filter == "expired":
            return row.status == "EXPIRED"
        return True

    rows = [row for row in candidates if visible(row)]
    events_by_symbol: dict[str, list[ResearchCandidateEvent]] = {}
    for event in events:
        events_by_symbol.setdefault(event.symbol, []).append(event)

    return templates.TemplateResponse(
        request,
        "admin/research.html",
        {
            "identity": identity,
            "active": "research",
            "rows": rows,
            "events_by_symbol": events_by_symbol,
            "trade_symbols": trade_symbols,
            "trade_by_symbol": trade_by_symbol,
            "selected_filter": selected_filter,
            **status_ctx,
        },
    )


async def _logs_page(
    request: Request, identity: str, *, error: str | None = None
) -> HTMLResponse:
    async with async_session_factory() as session:
        ai_decisions = await _latest(session, AiDecision, 20)
        risk_decisions = await _latest(session, RiskDecision, 20)
        order_logs = await _latest(session, OrderLog, 20)
        audit_logs = await _latest(session, ConfigAuditLog, 20)
        status_ctx = await _status_strip_context(session)

    return templates.TemplateResponse(
        request,
        "admin/logs.html",
        {
            "identity": identity,
            "active": "logs",
            "ai_decisions": ai_decisions,
            "risk_decisions": risk_decisions,
            "order_logs": order_logs,
            "audit_logs": audit_logs,
            "confirmation": RISKY_CONFIRMATION,
            "error": error,
            **status_ctx,
        },
    )


@admin_router.get("/logs", response_class=HTMLResponse)
async def admin_logs(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    return await _logs_page(request, identity)


def _log_table_or_404(table: str) -> Any:
    model = LOG_TABLES.get(table)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Unknown log table: {table}")
    return model


@admin_router.post("/logs/{table}/delete-all")
async def admin_logs_delete_all(request: Request, table: str) -> Any:
    identity = await require_admin(request)
    model = _log_table_or_404(table)
    form = await request.form()
    reason = str(form.get("reason") or "Delete all logs")
    confirmation = str(form.get("confirmation") or "")

    if confirmation != RISKY_CONFIRMATION:
        return await _logs_page(
            request,
            identity,
            error=f"delete-all requires confirmation={RISKY_CONFIRMATION}",
        )

    async with async_session_factory() as session:
        result = await session.execute(delete(model))
        await session.commit()

    logger.warning(
        "Admin %s deleted ALL %d rows from %s (reason: %s)",
        identity,
        result.rowcount or 0,
        table,
        reason,
    )
    return RedirectResponse("/admin/logs", status_code=status.HTTP_303_SEE_OTHER)


@admin_router.post("/logs/{table}/delete-selected")
async def admin_logs_delete_selected(request: Request, table: str) -> Any:
    identity = await require_admin(request)
    model = _log_table_or_404(table)
    form = await request.form()
    reason = str(form.get("reason") or "Delete selected logs")
    confirmation = str(form.get("confirmation") or "")
    ids = [int(raw) for raw in form.getlist("ids") if str(raw).strip().isdigit()]

    if confirmation != RISKY_CONFIRMATION:
        return await _logs_page(
            request,
            identity,
            error=f"delete-selected requires confirmation={RISKY_CONFIRMATION}",
        )
    if not ids:
        return await _logs_page(request, identity, error="Silinecek kayıt seçilmedi")

    async with async_session_factory() as session:
        result = await session.execute(delete(model).where(model.id.in_(ids)))
        await session.commit()

    logger.warning(
        "Admin %s deleted %d selected rows from %s (reason: %s)",
        identity,
        result.rowcount or 0,
        table,
        reason,
    )
    return RedirectResponse("/admin/logs", status_code=status.HTTP_303_SEE_OTHER)


@admin_router.get("/logs/{request_id}", response_class=HTMLResponse)
async def admin_log_detail(request: Request, request_id: str) -> HTMLResponse:
    """Everything recorded for one evaluation: exact payload sent to the AI,
    its raw response, the risk-engine's final decision, and any matching
    order result — all joined by requestId."""
    identity = await require_admin(request)
    async with async_session_factory() as session:
        snapshot = (
            await session.execute(
                select(MarketSnapshot).where(MarketSnapshot.request_id == request_id)
            )
        ).scalar_one_or_none()
        ai_decision = (
            await session.execute(
                select(AiDecision).where(AiDecision.request_id == request_id)
            )
        ).scalar_one_or_none()
        risk_decision = (
            await session.execute(
                select(RiskDecision).where(RiskDecision.request_id == request_id)
            )
        ).scalar_one_or_none()
        order_logs = (
            (
                await session.execute(
                    select(OrderLog)
                    .where(OrderLog.request_id == request_id)
                    .order_by(OrderLog.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        status_ctx = await _status_strip_context(session)

    def _pretty(value: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)

    return templates.TemplateResponse(
        request,
        "admin/log_detail.html",
        {
            "identity": identity,
            "active": "logs",
            "request_id": request_id,
            "snapshot": snapshot,
            "ai_decision": ai_decision,
            "risk_decision": risk_decision,
            "order_logs": order_logs,
            "raw_request_json": _pretty(ai_decision.raw_request)
            if ai_decision
            else None,
            "raw_response_json": _pretty(ai_decision.raw_response)
            if ai_decision
            else None,
            **status_ctx,
        },
    )


@admin_router.get("/emergency", response_class=HTMLResponse)
async def admin_emergency(request: Request) -> HTMLResponse:
    identity = await require_admin(request)
    async with async_session_factory() as session:
        configs = await _config_lookup(session)
        status_ctx = await _status_strip_context(session, configs=configs)

    kill_switch = configs["killSwitchEnabled"].value == "true"
    current_mode = configs["tradingMode"].value

    return templates.TemplateResponse(
        request,
        "admin/emergency.html",
        {
            "identity": identity,
            "active": "emergency",
            "configs": configs,
            "kill_switch": kill_switch,
            "current_mode": current_mode,
            "confirmation": RISKY_CONFIRMATION,
            "error": None,
            "message": None,
            "submitted_reason": "",
            **status_ctx,
        },
    )


@admin_router.post("/emergency/{action}")
async def admin_emergency_action(
    request: Request,
    action: str,
) -> Any:
    identity = await require_admin(request)
    form = await request.form()
    reason = str(form.get("reason") or f"Emergency action: {action}")
    confirmation = str(form.get("confirmation") or "")

    try:
        async with async_session_factory() as session:
            await _apply_emergency_action(
                session,
                action,
                changed_by=identity,
                reason=reason,
                confirmation=confirmation,
            )
    except ValueError as exc:
        async with async_session_factory() as session:
            configs = await _config_lookup(session)
            status_ctx = await _status_strip_context(session, configs=configs)
        kill_switch = configs["killSwitchEnabled"].value == "true"
        current_mode = configs["tradingMode"].value
        return templates.TemplateResponse(
            request,
            "admin/emergency.html",
            {
                "identity": identity,
                "active": "emergency",
                "configs": configs,
                "kill_switch": kill_switch,
                "current_mode": current_mode,
                "confirmation": RISKY_CONFIRMATION,
                "error": str(exc),
                "message": None,
                "submitted_reason": reason,
                **status_ctx,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return RedirectResponse("/admin/emergency", status_code=status.HTTP_303_SEE_OTHER)


@admin_api_router.get("/dashboard")
async def admin_api_dashboard(request: Request) -> dict[str, Any]:
    await require_admin(request)
    async with async_session_factory() as session:
        configs = await _config_lookup(session)
        today_counts = await get_today_trade_counts(session, "*")
        latest_risk = await _latest(session, RiskDecision, 20)
        latest_orders = await _latest(session, OrderLog, 20)

    return {
        "tradingMode": configs["tradingMode"].value,
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


@admin_api_router.get("/config")
async def admin_api_config(request: Request) -> list[dict[str, Any]]:
    await require_admin(request)
    async with async_session_factory() as session:
        configs = await list_admin_configs(session)
    return [_config_dict(item) for item in configs if not item.is_sensitive]


@admin_api_router.put("/config/{key}")
async def admin_api_update_config(
    request: Request,
    key: str,
    body: AdminConfigUpdate,
) -> dict[str, Any]:
    identity = await require_admin(request)
    try:
        async with async_session_factory() as session:
            item = await set_admin_config_value(
                session,
                key,
                body.value,
                changed_by=identity,
                reason=body.reason,
                confirmation=body.confirmation,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _notify_gateway_config_reload()
    if key in {"killSwitchEnabled", "tradingMode"} or item.requires_confirmation:
        await notification_service.send(
            "warning",
            "Yönetim yapılandırması değişti",
            {"Anahtar": key, "Değer": item.display_value, "Kullanıcı": identity},
            event_key=f"admin-config:{key}:{item.display_value}",
        )
    return _config_dict(item)


@admin_api_router.put("/config")
async def admin_api_update_config_batch(
    request: Request, body: AdminConfigBatchUpdate
) -> dict[str, Any]:
    identity = await require_admin(request)
    try:
        async with async_session_factory() as session:
            items = await set_admin_config_values(
                session,
                body.values,
                changed_by=identity,
                reason=body.reason,
                confirmation=body.confirmation,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _notify_gateway_config_reload()
    return {"status": "ok", "updated": [_config_dict(item) for item in items]}


@admin_api_router.post("/emergency/{action}")
async def admin_api_emergency(
    request: Request,
    action: str,
    body: EmergencyAction | None = None,
) -> dict[str, str]:
    identity = await require_admin(request)
    payload = body or EmergencyAction()
    try:
        async with async_session_factory() as session:
            await _apply_emergency_action(
                session,
                action,
                changed_by=identity,
                reason=payload.reason or f"Emergency action: {action}",
                confirmation=payload.confirmation,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await notification_service.send(
        "warning",
        "Acil işlem uygulandı",
        {"İşlem": action, "Kullanıcı": identity},
        event_key=f"admin-emergency:{action}",
    )
    return {"status": "ok", "action": action}


def _trade_profile_dict(profile: TradeProfile, active_code: str) -> dict[str, Any]:
    return {
        "code": profile.code,
        "name": profile.name,
        "description": profile.description,
        "riskLevel": profile.risk_level,
        "isEnabled": profile.is_enabled,
        "isDefault": profile.is_default,
        "isBuiltin": profile.is_builtin,
        "isActive": profile.code == active_code,
        "allowedModes": profile.allowed_modes,
        "maxOrderValueTl": profile.max_order_value_tl,
        "maxQtyPerOrder": profile.max_qty_per_order,
        "maxPositionValuePerSymbol": profile.max_position_value_per_symbol,
        "riskPerTradePct": profile.risk_per_trade_pct,
        "maxCashUtilizationPct": profile.max_cash_utilization_pct,
        "maxAccountExposurePct": profile.max_account_exposure_pct,
        "minOrderValueTl": profile.min_order_value_tl,
        "minStopDistancePct": profile.min_stop_distance_pct,
        "maxStopDistancePct": profile.max_stop_distance_pct,
        "minimumStopSlippagePct": profile.minimum_stop_slippage_pct,
        "maximumStopSlippagePct": profile.maximum_stop_slippage_pct,
        "profileStopSlippagePct": profile.profile_stop_slippage_pct,
        "maxAccountDataAgeSeconds": profile.max_account_data_age_seconds,
        "maxOrdersPerDay": profile.max_orders_per_day,
        "maxOrdersPerSymbolPerDay": profile.max_orders_per_symbol_per_day,
        "minConfidenceForBuy": profile.min_confidence_for_buy,
        "minConfidenceForSell": profile.min_confidence_for_sell,
        "maxNatrForBuy": profile.max_natr_for_buy,
        "maxDepthQueueDropPctForBuy": profile.max_depth_queue_drop_pct_for_buy,
        "maxSpreadPctForBuy": profile.max_spread_pct_for_buy,
        "minDepthBidAskRatioTop10ForBuy": profile.min_depth_bid_ask_ratio_top10_for_buy,
        "maxDepthSellPressureScoreForBuy": profile.max_depth_sell_pressure_score_for_buy,
        "blockBuyOnStrongSellPressure": profile.block_buy_on_strong_sell_pressure,
        "blockBuyOnNearAskWall": profile.block_buy_on_near_ask_wall,
        "nearWallDistancePct": profile.near_wall_distance_pct,
        "requireAlphaTrendAlignment": profile.require_alpha_trend_alignment,
        "requireIndicatorConsensusAlignment": profile.require_indicator_consensus_alignment,
        "allowSellLongTerm": profile.allow_sell_long_term,
        "allowShortSelling": profile.allow_short_selling,
        "allowRealLive": profile.allow_real_live,
        "allowDemoLive": profile.allow_demo_live,
        "allowMarginBuying": profile.allow_margin_buying,
        "scanIntervalMinutes": profile.scan_interval_minutes,
        "maxFetchLoopPerSession": profile.max_fetch_loop_per_session,
        "orderTimeInForce": profile.order_time_in_force,
        "indicatorPeriod": profile.indicator_period,
    }


@admin_api_router.get("/trade-profiles")
async def admin_api_list_trade_profiles(request: Request) -> list[dict[str, Any]]:
    await require_admin(request)
    async with async_session_factory() as session:
        profiles = await list_profiles(session)
        active = await get_active_profile(session)
    return [_trade_profile_dict(p, active.code) for p in profiles]


@admin_api_router.post("/trade-profiles")
async def admin_api_create_trade_profile(
    request: Request, body: TradeProfileCreateBody
) -> dict[str, Any]:
    identity = await require_admin(request)
    fields = body.model_dump(
        exclude={"code", "name", "description", "risk_level"}, exclude_none=True
    )
    try:
        async with async_session_factory() as session:
            profile = await create_profile(
                session,
                code=body.code,
                name=body.name or body.code,
                description=body.description or "",
                risk_level=body.risk_level or "MEDIUM",
                changed_by=identity,
                **fields,
            )
            active = await get_active_profile(session)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _notify_gateway_config_reload()
    return _trade_profile_dict(profile, active.code)


@admin_api_router.get("/trade-profiles/{code}")
async def admin_api_get_trade_profile(request: Request, code: str) -> dict[str, Any]:
    await require_admin(request)
    async with async_session_factory() as session:
        profile = await get_profile(session, code)
        if profile is None:
            raise HTTPException(
                status_code=404, detail=f"Unknown trade profile: {code}"
            )
        active = await get_active_profile(session)
    return _trade_profile_dict(profile, active.code)


@admin_api_router.put("/trade-profiles/{code}")
async def admin_api_update_trade_profile(
    request: Request, code: str, body: TradeProfileUpdateBody
) -> dict[str, Any]:
    identity = await require_admin(request)
    changes = body.model_dump(exclude={"reason", "confirmation"}, exclude_none=True)
    try:
        async with async_session_factory() as session:
            profile = await update_profile(
                session,
                code,
                changes,
                changed_by=identity,
                reason=body.reason,
                confirmation=body.confirmation,
            )
            active = await get_active_profile(session)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _notify_gateway_config_reload()
    return _trade_profile_dict(profile, active.code)


@admin_api_router.post("/trade-profiles/{code}/activate")
async def admin_api_activate_trade_profile(
    request: Request, code: str, body: TradeProfileActivateBody | None = None
) -> dict[str, Any]:
    identity = await require_admin(request)
    payload = body or TradeProfileActivateBody()
    try:
        async with async_session_factory() as session:
            profile = await activate_profile(
                session,
                code,
                changed_by=identity,
                reason=payload.reason,
                confirmation=payload.confirmation,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _notify_gateway_config_reload()
    return _trade_profile_dict(profile, code)


@admin_api_router.post("/trade-profiles/{code}/clone")
async def admin_api_clone_trade_profile(
    request: Request, code: str, body: TradeProfileCloneBody
) -> dict[str, Any]:
    identity = await require_admin(request)
    try:
        async with async_session_factory() as session:
            clone = await clone_profile(
                session,
                code,
                new_code=body.new_code,
                new_name=body.new_name,
                changed_by=identity,
            )
            active = await get_active_profile(session)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _notify_gateway_config_reload()
    return _trade_profile_dict(clone, active.code)


@admin_api_router.post("/trade-profiles/{code}/disable")
async def admin_api_disable_trade_profile(
    request: Request, code: str
) -> dict[str, Any]:
    identity = await require_admin(request)
    try:
        async with async_session_factory() as session:
            profile = await disable_profile(session, code, changed_by=identity)
            active = await get_active_profile(session)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _notify_gateway_config_reload()
    return _trade_profile_dict(profile, active.code)


@admin_api_router.delete("/trade-profiles/{code}")
async def admin_api_delete_trade_profile(request: Request, code: str) -> dict[str, str]:
    identity = await require_admin(request)
    try:
        async with async_session_factory() as session:
            await delete_profile(session, code, changed_by=identity)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _notify_gateway_config_reload()
    return {"status": "ok", "code": code}


def _fundamental_dict(row: Any) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "period": row.period,
        "fcfGrowthPct": row.fcf_growth_pct,
        "debtToEquity": row.debt_to_equity,
        "netMarginPct": row.net_margin_pct,
        "netMarginChangePt": row.net_margin_change_pt,
        "revenueGrowthPct": row.revenue_growth_pct,
        "notes": row.notes,
        "updatedBy": row.updated_by,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


@admin_api_router.get("/fundamentals")
async def admin_api_list_fundamentals(request: Request) -> list[dict[str, Any]]:
    await require_admin(request)
    async with async_session_factory() as session:
        rows = await list_fundamentals(session)
    return [_fundamental_dict(row) for row in rows]


@admin_api_router.put("/fundamentals/{symbol}")
async def admin_api_upsert_fundamental(
    request: Request, symbol: str, body: FundamentalBody
) -> dict[str, Any]:
    identity = await require_admin(request)
    try:
        async with async_session_factory() as session:
            row = await upsert_fundamental(
                session,
                symbol,
                period=body.period,
                changed_by=identity,
                notes=body.notes,
                fcf_growth_pct=body.fcf_growth_pct,
                debt_to_equity=body.debt_to_equity,
                net_margin_pct=body.net_margin_pct,
                net_margin_change_pt=body.net_margin_change_pt,
                revenue_growth_pct=body.revenue_growth_pct,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _fundamental_dict(row)


@admin_api_router.delete("/fundamentals/{symbol}")
async def admin_api_delete_fundamental(request: Request, symbol: str) -> dict[str, str]:
    await require_admin(request)
    async with async_session_factory() as session:
        existed = await delete_fundamental(session, symbol)
    if not existed:
        raise HTTPException(status_code=404, detail=f"No fundamentals for {symbol}")
    return {"status": "ok", "symbol": symbol.strip().upper()}


async def _config_lookup(session: Any) -> dict[str, AdminConfigItem]:
    return {item.key: item for item in await list_admin_configs(session)}


async def _status_strip_context(
    session: Any,
    *,
    configs: dict[str, AdminConfigItem] | None = None,
    profile: TradeProfile | None = None,
) -> dict[str, Any]:
    """Trading mode / kill switch / active trade profile — shown in the
    header on every admin page so the current risk posture is always
    visible without needing to visit Dashboard or Trade Profiles first."""
    configs = configs if configs is not None else await _config_lookup(session)
    profile = profile if profile is not None else await get_active_profile(session)
    return {
        "status_mode": configs["tradingMode"].value,
        "status_kill_switch": configs["killSwitchEnabled"].value == "true",
        "status_profile_code": profile.code,
        "status_profile_risk_level": profile.risk_level,
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
        status_ctx = {
            "status_mode": "UNKNOWN",
            "status_kill_switch": False,
            "status_profile_code": "UNKNOWN",
            "status_profile_risk_level": "UNKNOWN",
        }
        db_error = str(exc)
    else:
        today_trade_count = today_counts.bot_count
    return {
        "configs": configs,
        "active_profile": active_profile,
        "today_trade_count": today_trade_count,
        "latest_risk": latest_risk,
        "latest_orders": latest_orders,
        "latest_account_normalization": latest_account[0] if latest_account else None,
        "bot_status": await _bot_status(db_error=db_error),
        "dashboard_db_error": db_error,
        **status_ctx,
    }


async def _replay_page_context() -> tuple[
    list[TradeProfile], dict[str, Any], str | None
]:
    """Keep replay diagnostics available even when the database is transiently down."""
    try:
        async with async_session_factory() as session:
            profiles = await list_profiles(session)
            status_ctx = await _status_strip_context(session)
        return profiles, status_ctx, None
    except Exception as exc:
        logger.warning("Replay page DB query failed: %s", exc)
        return (
            [],
            {
                "status_mode": "UNKNOWN",
                "status_kill_switch": False,
                "status_profile_code": "UNKNOWN",
                "status_profile_risk_level": "UNKNOWN",
            },
            f"Database unavailable: {exc}",
        )


async def _bot_status(*, db_error: str | None = None) -> dict[str, Any]:
    """Collect gateway, scanner and runtime state; every source is optional."""
    result: dict[str, Any] = {
        "gateway": {"reachable": False, "health": None, "error": None},
        "scanner": scanner.get_status(),
        "positionSync": position_synchronizer.get_status(),
        "runtime": {"dbAvailable": db_error is None, "dbError": db_error},
    }
    try:
        health = await gateway_client.health()
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
                "tradingMode": config_values.get("tradingMode", "UNKNOWN"),
                "botMode": config_values.get("botMode", "UNKNOWN"),
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
    return result


async def _apply_emergency_action(
    session: Any,
    action: str,
    *,
    changed_by: str,
    reason: str,
    confirmation: str | None,
) -> None:
    if action == "force-paper":
        await set_admin_config_value(
            session,
            "tradingMode",
            "PAPER",
            changed_by=changed_by,
            reason=reason,
            confirmation=confirmation,
        )
        return
    if action == "enable-kill-switch":
        await set_admin_config_value(
            session,
            "killSwitchEnabled",
            True,
            changed_by=changed_by,
            reason=reason,
            confirmation=confirmation,
        )
        return
    if action == "disable-kill-switch":
        await set_admin_config_value(
            session,
            "killSwitchEnabled",
            False,
            changed_by=changed_by,
            reason=reason,
            confirmation=confirmation,
        )
        return
    raise ValueError(f"Unsupported emergency action: {action}")


async def _latest(
    session: Any,
    model: Any,
    limit: int,
    *,
    order_field: str = "created_at",
) -> list[Any]:
    column = getattr(model, order_field)
    stmt = select(model).order_by(column.desc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _config_dict(item: AdminConfigItem) -> dict[str, Any]:
    return {
        "key": item.key,
        "value": item.display_value,
        "valueType": item.value_type,
        "description": item.description,
        "isSensitive": item.is_sensitive,
        "source": item.source,
        "updatedAt": item.updated_at.isoformat() if item.updated_at else None,
    }


def _row_dict(row: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        result[column.name] = value
    return result


def _make_admin_cookie() -> str:
    expires = int(time.time()) + ADMIN_COOKIE_TTL_SECONDS
    payload = f"admin|{expires}"
    signature = _sign(payload)
    return f"{payload}|{signature}"


def _verify_admin_cookie(cookie: str) -> bool:
    parts = cookie.split("|")
    if len(parts) != 3:
        return False
    user, expires_raw, signature = parts
    if user != "admin":
        return False
    try:
        expires = int(expires_raw)
    except ValueError:
        return False
    if expires < int(time.time()):
        return False
    payload = f"{user}|{expires}"
    return hmac.compare_digest(signature, _sign(payload))


def _sign(payload: str) -> str:
    secret = f"{settings.admin_password}:{settings.effective_admin_api_token}".encode(
        "utf-8"
    )
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
