"""Bot-facing config endpoints — tradeable symbol universe and position sync."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.auth import verify_token
from app.core.risk_config import risk_config
from app.db.session import async_session_factory
from app.models.db import BotPosition
from app.services.admin_config import build_runtime_risk_config
from app.services.bot_runtime_config import (
    build_bot_runtime_config,
    build_static_bot_runtime_config,
)
from app.services.signal_override import list_pending_override_symbols

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Bot"], dependencies=[Depends(verify_token)])


def _split_symbols(raw: str) -> list[str]:
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


# ── GET /bot/tradeable-symbols ───────────────────────────────────────────────


class TradeableSymbolsResponse(BaseModel):
    symbols: list[str]
    locked_long_term: list[str] = Field(alias="lockedLongTerm")

    model_config = {"populate_by_name": True}


class BotRuntimeConfigResponse(BaseModel):
    config_version: str = Field(alias="configVersion")
    config_hash: str = Field(alias="configHash")
    mode: str
    enable_demo_orders: bool = Field(alias="enableDemoOrders")
    enable_real_orders: bool = Field(alias="enableRealOrders")
    require_demo_account: bool = Field(alias="requireDemoAccount")
    demo_account_confirmed: bool = Field(alias="demoAccountConfirmed")
    max_order_value_tl: float = Field(alias="maxOrderValueTl")
    max_qty_per_order: float = Field(alias="maxQtyPerOrder")
    max_orders_per_day: int = Field(alias="maxOrdersPerDay")
    max_orders_per_symbol_per_day: int = Field(alias="maxOrdersPerSymbolPerDay")
    allow_market_orders: bool = Field(alias="allowMarketOrders")
    scan_interval_minutes: int = Field(alias="scanIntervalMinutes")
    http_timeout_seconds: int = Field(alias="httpTimeoutSeconds")
    max_fetch_loop_per_session: int = Field(alias="maxFetchLoopPerSession")
    order_time_in_force: str = Field(alias="orderTimeInForce")
    indicator_period: str = Field(alias="indicatorPeriod")
    allowed_symbols: list[str] = Field(alias="allowedSymbols")
    locked_long_term_qty: dict[str, float] = Field(alias="lockedLongTermQty")

    model_config = {"populate_by_name": True}


@router.get("/bot/tradeable-symbols")
async def get_tradeable_symbols() -> TradeableSymbolsResponse:
    """Return the admin-managed symbol universe the bot should scan.

    Falls back to the static ``.env``-backed config if the DB is unreachable,
    so the bot can still start up with a sane default.
    """
    try:
        async with async_session_factory() as session:
            cfg = await build_runtime_risk_config(session)
    except Exception:
        logger.exception("Failed to load runtime risk config, using static defaults")
        cfg = risk_config

    return TradeableSymbolsResponse(
        symbols=_split_symbols(cfg.allowed_symbols),
        lockedLongTerm=_split_symbols(cfg.locked_long_term_symbols),
    )


@router.get("/bot/config")
async def get_bot_runtime_config() -> BotRuntimeConfigResponse:
    """Return the full server-driven runtime config for the Matriks bot."""
    try:
        async with async_session_factory() as session:
            config = await build_bot_runtime_config(session)
    except Exception:
        logger.exception("Failed to load bot runtime config, using static fallback")
        config = build_static_bot_runtime_config()

    return BotRuntimeConfigResponse(**config)


# ── POST /bot/positions/sync ─────────────────────────────────────────────────


class PositionEntry(BaseModel):
    symbol: str
    qty: float


class PositionSyncRequest(BaseModel):
    positions: list[PositionEntry]


class PositionSyncResponse(BaseModel):
    status: str
    synced: int


@router.post("/bot/positions/sync")
async def sync_bot_positions(body: PositionSyncRequest) -> PositionSyncResponse:
    """Upsert the bot's full position snapshot into ``bot_positions``.

    Symbols not included in this snapshot are left untouched (the bot may
    only report positions it currently holds market data for).
    """
    synced = 0
    try:
        async with async_session_factory() as session:
            for entry in body.positions:
                symbol = entry.symbol.strip().upper()
                if not symbol:
                    continue
                stmt = select(BotPosition).where(BotPosition.symbol == symbol)
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    session.add(BotPosition(symbol=symbol, qty=entry.qty))
                else:
                    row.qty = entry.qty
                synced += 1
            await session.commit()
    except Exception:
        logger.exception("Failed to sync bot positions")

    return PositionSyncResponse(status="ok", synced=synced)


# ── GET /bot/pending-overrides ───────────────────────────────────────────────


class PendingOverridesResponse(BaseModel):
    symbols: list[str]


@router.get("/bot/pending-overrides")
async def get_pending_overrides() -> PendingOverridesResponse:
    """Return symbols with a pending manual override.

    The bot polls this every timer tick so it can scan a symbol immediately
    instead of waiting out the normal ScanIntervalMinutes interval — this is
    a read-only peek, the override itself is only consumed when the bot's
    real evaluate-agent request for that symbol arrives.
    """
    try:
        async with async_session_factory() as session:
            symbols = await list_pending_override_symbols(session)
    except Exception:
        logger.exception("Failed to load pending overrides")
        symbols = []

    return PendingOverridesResponse(symbols=symbols)
