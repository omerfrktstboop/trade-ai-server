"""Daily trade count helpers for risk checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.models.db import OrderLog, RiskDecision

TRADE_ACTIONS = ("BUY", "SELL")
IGNORED_ORDER_STATUSES = ("CANCELED", "CANCELLED", "REJECTED", "FAILED", "ERROR")
TRADING_TIMEZONE = timezone(timedelta(hours=3), name="TRT")


@dataclass(frozen=True)
class DailyTradeCounts:
    """Trade counts for the current trading day."""

    symbol: str
    symbol_count: int
    bot_count: int

    @property
    def effective_count(self) -> int:
        """Conservative count used by the risk engine."""
        return max(self.symbol_count, self.bot_count)


async def get_today_trade_counts(
    session: AsyncSession,
    symbol: str,
    *,
    now: datetime | None = None,
) -> DailyTradeCounts:
    """Return today's trade count for the symbol and the whole bot.

    ``order_logs`` records broker-side order results. ``risk_decisions`` is a
    fallback signal for allowed BUY/SELL decisions when order results have not
    been written yet. The two sources are combined by ``request_id`` so the same
    order is not double-counted when both tables have a row for it.
    """
    symbol_normalized = symbol.strip().upper()
    start_of_day = _start_of_trading_day(now)

    symbol_count = await _count_trade_requests(
        session, start_of_day, symbol=symbol_normalized
    )
    bot_count = await _count_trade_requests(session, start_of_day)

    return DailyTradeCounts(
        symbol=symbol_normalized,
        symbol_count=symbol_count,
        bot_count=bot_count,
    )


def _start_of_trading_day(now: datetime | None = None) -> datetime:
    """Return the start of today in the BIST trading timezone."""
    current = now or datetime.now(TRADING_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TRADING_TIMEZONE)
    current = current.astimezone(TRADING_TIMEZONE)
    return datetime.combine(current.date(), time.min).replace(tzinfo=TRADING_TIMEZONE)


async def _count_trade_requests(
    session: AsyncSession,
    start_of_day: datetime,
    *,
    symbol: str | None = None,
) -> int:
    combined = _order_log_request_ids(start_of_day, symbol=symbol).union(
        _risk_decision_request_ids(start_of_day, symbol=symbol)
    )
    stmt = select(func.count()).select_from(combined.subquery())
    result = await session.execute(stmt)
    return int(result.scalar_one() or 0)


def _order_log_request_ids(
    start_of_day: datetime,
    *,
    symbol: str | None = None,
) -> Select[tuple[str]]:
    status = func.upper(func.coalesce(OrderLog.status, ""))
    stmt = select(OrderLog.request_id).where(
        OrderLog.created_at >= start_of_day,
        func.upper(OrderLog.action).in_(TRADE_ACTIONS),
        ~status.in_(IGNORED_ORDER_STATUSES),
    )
    if symbol:
        stmt = stmt.where(func.upper(OrderLog.symbol) == symbol)

    return stmt


def _risk_decision_request_ids(
    start_of_day: datetime,
    *,
    symbol: str | None = None,
) -> Select[tuple[str]]:
    stmt = select(RiskDecision.request_id).where(
        RiskDecision.created_at >= start_of_day,
        func.upper(RiskDecision.action).in_(TRADE_ACTIONS),
        RiskDecision.allow_order.is_(True),
    )
    if symbol:
        stmt = stmt.where(func.upper(RiskDecision.symbol) == symbol)

    return stmt
