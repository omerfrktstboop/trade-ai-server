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
