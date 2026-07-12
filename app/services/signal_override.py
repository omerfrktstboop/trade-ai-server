"""Admin-injected one-shot signal overrides — bypass the AI provider for testing.

Used by ``evaluate_signal_agent`` (app/routers/signal.py) to let an admin
force a BUY/SELL decision for a specific symbol's next evaluation, while
still running the full RiskEngine safety pipeline (cutoff time, daily
limit, symbol allow-list, mode-based allowOrder, SELL qty clamp, etc.).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import SignalOverride

# Sentinel SELL qty — large enough that RiskEngine's SELL clamp
# (min(botPositionQty, totalAccountQty - lockedLongTermQty)) always reduces
# it down to whatever is actually sellable.
SELL_ALL_SENTINEL_QTY = 1_000_000_000.0

DEFAULT_TTL_MINUTES = 30


async def create_override(
    session: AsyncSession,
    symbol: str,
    action: str,
    qty: float,
    *,
    reason: str,
    created_by: str,
    entry_min: float | None = None,
    entry_max: float | None = None,
    stop_loss: float | None = None,
    target_price: float | None = None,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
) -> SignalOverride:
    """Create (or replace) the pending override for a symbol."""
    symbol = symbol.strip().upper()
    action = action.strip().upper()
    if action not in ("BUY", "SELL"):
        raise ValueError(f"Unsupported override action: {action}")

    stmt = select(SignalOverride).where(SignalOverride.symbol == symbol)
    row = (await session.execute(stmt)).scalar_one_or_none()

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)

    if row is None:
        row = SignalOverride(symbol=symbol)
        session.add(row)

    row.action = action
    row.confidence = 100.0
    row.qty = qty
    row.entry_min = entry_min
    row.entry_max = entry_max
    row.stop_loss = stop_loss
    row.target_price = target_price
    row.reason = reason
    row.created_by = created_by
    row.expires_at = expires_at

    await session.commit()
    await session.refresh(row)
    return row


async def consume_override(session: AsyncSession, symbol: str) -> SignalOverride | None:
    """Fetch and delete the active override for a symbol, if any.

    Returns ``None`` when there is no override or it has expired (an
    expired row is deleted too, so it doesn't linger).
    """
    symbol = symbol.strip().upper()
    stmt = select(SignalOverride).where(SignalOverride.symbol == symbol)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None

    await session.delete(row)
    await session.commit()

    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        return None

    return row


async def list_pending_override_symbols(session: AsyncSession) -> list[str]:
    """Peek at symbols with a non-expired pending override.

    Read-only — does not consume/delete anything, so the bot can poll this
    cheaply every timer tick to know which symbols to scan immediately
    instead of waiting out the normal ScanIntervalMinutes wait.
    """
    now = datetime.now(timezone.utc)
    stmt = select(SignalOverride.symbol, SignalOverride.expires_at)
    rows = (await session.execute(stmt)).all()

    symbols: list[str] = []
    for symbol, expires_at in rows:
        exp = (
            expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
        )
        if exp > now:
            symbols.append(symbol)
    return symbols


def override_to_raw_decision(override: SignalOverride) -> dict[str, Any]:
    """Build a provider-shaped ``raw`` dict from an override.

    Matches what ``_dict_to_risk_decision`` (app/routers/signal.py) expects
    from a real AI provider response.
    """
    raw: dict[str, Any] = {
        "action": override.action,
        "confidence": override.confidence,
        "risk_score": 0.0,
        "qty": override.qty,
        "reason": f"Manual test override by {override.created_by}: {override.reason}",
    }
    if override.entry_min is not None and override.entry_max is not None:
        raw["entry_range"] = {"min": override.entry_min, "max": override.entry_max}
    if override.stop_loss is not None:
        raw["stop_loss"] = override.stop_loss
    if override.target_price is not None:
        raw["target_price"] = override.target_price
    return raw
