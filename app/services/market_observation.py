"""Collects MarketObservation rows from snapshot payloads the scanner and
stop-loss guard already fetched for their own purposes - never opens a new
gateway request (Task 3.2). Best-effort and non-blocking: a persistence
failure here must never affect evaluation or order dispatch.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.db import MarketObservation
from app.services.fill_ledger import to_decimal

logger = logging.getLogger(__name__)

SERVER_OBSERVED_AT = "SERVER_OBSERVED_AT"

# Preference order: tick-level quote event, then bar event, then the
# snapshot-assembly timestamp - all real gateway-reported times, before
# falling back to server retrieval time.
_TIMESTAMP_FIELDS = ("quoteEventUtc", "barEventUtc", "snapshotBuiltUtc")


def _resolve_observed_at(payload: dict[str, Any]) -> tuple[datetime, str]:
    for field in _TIMESTAMP_FIELDS:
        raw = payload.get(field)
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed, field
    return datetime.now(timezone.utc), SERVER_OBSERVED_AT


def _bar_prefixed(payload: dict[str, Any], key: str) -> Decimal | None:
    """open/high/low prefer the plain field; close only exists as barClose -
    lastPrice is the live tick, not a bar close, and must not be conflated
    with it (Task 3.2's ohlcReliable distinction)."""
    value = payload.get(key)
    if value is None:
        value = payload.get("bar" + key[0].upper() + key[1:])
    return to_decimal(value)


async def record_market_observation(
    session: AsyncSession,
    symbol: str,
    payload: dict[str, Any],
    *,
    request_id: str | None = None,
) -> MarketObservation | None:
    """Idempotent insert; returns None (not an error) on a duplicate key or
    any persistence failure - this is a measurement side-channel."""
    try:
        observed_at, observed_at_source = _resolve_observed_at(payload)
        price_source = payload.get("priceSource")
        values = dict(
            symbol=symbol.strip().upper(),
            observed_at=observed_at,
            observed_at_source=observed_at_source,
            last_price=to_decimal(payload.get("lastPrice")),
            open=_bar_prefixed(payload, "open"),
            high=_bar_prefixed(payload, "high"),
            low=_bar_prefixed(payload, "low"),
            close=to_decimal(payload.get("barClose")),
            bar_period=payload.get("actualBarPeriod") or payload.get("timeframe"),
            bar_closed=payload.get("barClosed"),
            quote_reliable=payload.get("quoteReliable"),
            ohlc_reliable=payload.get("ohlcReliable"),
            quote_age_seconds=to_decimal(payload.get("quoteAgeSeconds")),
            ohlcv_age_seconds=to_decimal(payload.get("ohlcvAgeSeconds")),
            price_source=price_source,
            request_id=request_id,
        )
        dialect = session.bind.dialect.name
        statement = (
            (pg_insert(MarketObservation) if dialect == "postgresql" else sqlite_insert(MarketObservation))
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["symbol", "observed_at", "bar_period", "price_source"]
            )
        )
        await session.execute(statement)
        await session.flush()
        return None
    except Exception:
        logger.exception("MARKET_OBSERVATION_RECORD_FAILED symbol=%s", symbol)
        return None


async def record_market_observation_standalone(
    symbol: str, payload: dict[str, Any], *, request_id: str | None = None
) -> None:
    """Same as record_market_observation, but opens and commits its own
    session - for call sites (stop-loss guard, evaluate_symbol's initial
    snapshot) that do not already have one open at the point a snapshot is
    fetched. Never raises."""
    try:
        async with async_session_factory() as session:
            await record_market_observation(session, symbol, payload, request_id=request_id)
            await session.commit()
    except Exception:
        logger.exception("MARKET_OBSERVATION_STANDALONE_RECORD_FAILED symbol=%s", symbol)
