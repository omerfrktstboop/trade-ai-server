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
    symbol_accepted_order_count: int = 0
    bot_accepted_order_count: int = 0
    symbol_filled_order_count: int = 0
    bot_filled_order_count: int = 0

    @property
    def effective_count(self) -> int:
        """Conservative count used by the risk engine."""
        return max(self.symbol_count, self.bot_count)


@dataclass(frozen=True)
class DailyOrderCountMaps:
    accepted_by_symbol: dict[str, int]
    filled_by_symbol: dict[str, int]
    reserved_or_sent_by_symbol: dict[str, int]


async def get_today_order_count_maps(
    session: AsyncSession, *, now: datetime | None = None
) -> DailyOrderCountMaps:
    """Return restart-safe unique request counts for the gateway config."""
    start_of_day = _start_of_trading_day(now)
    result = await session.execute(
        select(
            OrderLog.request_id,
            OrderLog.symbol,
            OrderLog.state,
            OrderLog.status,
            OrderLog.filled_qty,
        ).where(
            OrderLog.created_at >= start_of_day,
            func.upper(OrderLog.action).in_(TRADE_ACTIONS),
        )
    )
    accepted_states = {
        "SENT_PENDING",
        "NEW",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCEL_REQUESTED",
        "CANCELED",
        "CANCELLED",
        "EXPIRED",
    }
    ignored_states = {"REJECTED", "FAILED", "ERROR"}
    accepted: dict[str, set[str]] = {}
    filled: dict[str, set[str]] = {}
    reserved: dict[str, set[str]] = {}
    for request_id, symbol_raw, state_raw, status_raw, filled_qty in result.all():
        symbol = str(symbol_raw).strip().upper()
        state = str(state_raw or status_raw or "").strip().upper()
        if state not in ignored_states:
            reserved.setdefault(symbol, set()).add(str(request_id))
        if state in accepted_states:
            accepted.setdefault(symbol, set()).add(str(request_id))
        if float(filled_qty or 0) > 0 or state in {"PARTIALLY_FILLED", "FILLED"}:
            filled.setdefault(symbol, set()).add(str(request_id))
    return DailyOrderCountMaps(
        accepted_by_symbol={key: len(value) for key, value in accepted.items()},
        filled_by_symbol={key: len(value) for key, value in filled.items()},
        reserved_or_sent_by_symbol={key: len(value) for key, value in reserved.items()},
    )


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
    symbol_accepted = await _count_order_logs(
        session, start_of_day, symbol=symbol_normalized, filled_only=False
    )
    bot_accepted = await _count_order_logs(session, start_of_day, filled_only=False)
    symbol_filled = await _count_order_logs(
        session, start_of_day, symbol=symbol_normalized, filled_only=True
    )
    bot_filled = await _count_order_logs(session, start_of_day, filled_only=True)

    return DailyTradeCounts(
        symbol=symbol_normalized,
        symbol_count=symbol_count,
        bot_count=bot_count,
        symbol_accepted_order_count=symbol_accepted,
        bot_accepted_order_count=bot_accepted,
        symbol_filled_order_count=symbol_filled,
        bot_filled_order_count=bot_filled,
    )


async def _count_order_logs(
    session: AsyncSession,
    start_of_day: datetime,
    *,
    symbol: str | None = None,
    filled_only: bool,
) -> int:
    """Count unique persisted order request IDs, never process memory."""
    status = func.upper(func.coalesce(OrderLog.state, OrderLog.status, ""))
    stmt = select(func.count(func.distinct(OrderLog.request_id))).where(
        OrderLog.created_at >= start_of_day,
        func.upper(OrderLog.action).in_(TRADE_ACTIONS),
    )
    if filled_only:
        stmt = stmt.where(
            (func.coalesce(OrderLog.filled_qty, 0) > 0)
            | status.in_(("PARTIALLY_FILLED", "FILLED"))
        )
    else:
        # This field is intentionally narrower than ``effective_count``:
        # SEND_UNKNOWN is reserved by the conservative risk counter but is not
        # mislabeled as broker-accepted.
        stmt = stmt.where(
            status.in_(
                (
                    "SENT_PENDING",
                    "NEW",
                    "PARTIALLY_FILLED",
                    "FILLED",
                    "CANCEL_REQUESTED",
                    "CANCELED",
                    "CANCELLED",
                    "EXPIRED",
                )
            )
        )
    if symbol:
        stmt = stmt.where(func.upper(OrderLog.symbol) == symbol)
    result = await session.execute(stmt)
    return int(result.scalar_one() or 0)


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
