"""Applies one real OrderFill to its symbol's PositionLifecycle: opens a new
lifecycle on 0->positive qty, updates weighted average cost and realized P&L
on further fills, and closes the lifecycle when qty returns to zero
(Task 1.3). Also binds/tightens the position's stop-loss from the fill's
originating RiskDecision and writes the PositionStopEvent audit trail
(Task 4.1-4.4). Must be called from inside the same transaction that created
the OrderFill, using the row-locked OrderLog already held by the caller.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import OrderFill, OrderLog, PositionLifecycle, PositionStopEvent, RiskDecision
from app.services.fill_ledger import to_decimal
from app.services.strategy_provenance import PROMPT_VERSION, STRATEGY_VERSION

logger = logging.getLogger(__name__)

_MAX_OPEN_INSERT_ATTEMPTS = 3


class LifecycleIntegrityError(Exception):
    """More than one OPEN PositionLifecycle exists for a symbol.

    The partial unique index added in Task 2 makes this impossible for new
    data going forward; if it is ever raised, it means pre-existing/corrupted
    data was found. Callers must not guess which row is authoritative - they
    must stop the affected measurement update and surface a repair job.
    """

    def __init__(self, symbol: str, lifecycle_ids: list[int]) -> None:
        self.symbol = symbol
        self.lifecycle_ids = lifecycle_ids
        super().__init__(
            f"{len(lifecycle_ids)} OPEN lifecycles found for {symbol}: {lifecycle_ids}"
        )


async def get_open_lifecycle(
    session: AsyncSession, symbol: str, *, for_update: bool = False
) -> PositionLifecycle | None:
    """Return the single OPEN lifecycle for ``symbol``, or None.

    Raises LifecycleIntegrityError instead of silently picking one (Task
    2.2) if more than one OPEN row exists - that state should be prevented
    by the partial unique index, so seeing it means genuinely corrupted data
    that must go through repair, not a guess.
    """
    stmt = select(PositionLifecycle).where(
        PositionLifecycle.symbol == symbol.strip().upper(),
        PositionLifecycle.status == "OPEN",
    )
    if for_update:
        stmt = stmt.with_for_update()
    rows = (await session.execute(stmt)).scalars().all()
    if len(rows) > 1:
        ids = [row.id for row in rows]
        logger.error(
            "LIFECYCLE_INTEGRITY_DUPLICATE_OPEN symbol=%s count=%s ids=%s",
            symbol,
            len(rows),
            ids,
        )
        raise LifecycleIntegrityError(symbol, ids)
    return rows[0] if rows else None


async def _resolve_decision_stop_target(
    session: AsyncSession, request_id: str
) -> tuple[Decimal | None, Decimal | None]:
    """The stop/target recorded by the RiskDecision linked to this fill's
    order, via OrderLog.request_id == RiskDecision.request_id (Task 4.1)."""
    row = (
        await session.execute(
            select(RiskDecision.stop_loss, RiskDecision.target_price).where(
                RiskDecision.request_id == request_id
            )
        )
    ).first()
    if row is None:
        return None, None
    stop_loss = to_decimal(row[0])
    target_price = to_decimal(row[1])
    if stop_loss is not None and stop_loss <= 0:
        stop_loss = None
    if target_price is not None and target_price <= 0:
        target_price = None
    return stop_loss, target_price


async def _record_stop_event(
    session: AsyncSession,
    lifecycle: PositionLifecycle,
    *,
    old_stop: Decimal | None,
    new_stop: Decimal | None,
    event_type: str,
    source_request_id: str | None,
    source_order_id: str | None,
    reason: str,
) -> None:
    session.add(
        PositionStopEvent(
            position_lifecycle_id=lifecycle.id,
            symbol=lifecycle.symbol,
            old_stop=old_stop,
            new_stop=new_stop,
            event_type=event_type,
            source_request_id=source_request_id,
            source_order_id=source_order_id,
            reason=reason,
        )
    )


async def record_stop_breach(
    session: AsyncSession,
    lifecycle: PositionLifecycle,
    *,
    source_request_id: str | None,
    reason: str,
) -> None:
    """Called by the stop-loss guard the moment it decides a breach exit is
    warranted, before the exit order is dispatched (Task 4.4)."""
    await _record_stop_event(
        session,
        lifecycle,
        old_stop=lifecycle.active_stop_loss,
        new_stop=lifecycle.active_stop_loss,
        event_type="STOP_BREACHED",
        source_request_id=source_request_id,
        source_order_id=None,
        reason=reason,
    )
    await session.flush()


async def apply_fill_to_lifecycle(
    session: AsyncSession, row: OrderLog, fill: OrderFill
) -> PositionLifecycle | None:
    """Apply one just-recorded OrderFill to its symbol's lifecycle.

    Returns the affected lifecycle, or None if a SELL fill arrived with no
    open lifecycle to apply it to (logged - never fabricated).
    """
    symbol = fill.symbol.strip().upper()

    if fill.action == "BUY":
        return await _apply_buy_fill(session, row, fill, symbol)

    lifecycle = await get_open_lifecycle(session, symbol, for_update=True)
    return await _apply_sell_fill(session, fill, symbol, lifecycle)


async def _apply_buy_fill(
    session: AsyncSession, row: OrderLog, fill: OrderFill, symbol: str
) -> PositionLifecycle:
    """Open a new lifecycle or merge into the open one.

    Two BUY fills for the same symbol racing to open a brand new lifecycle
    (Task 2.2): the losing INSERT hits the partial unique open-lifecycle
    index inside its own SAVEPOINT, which rolls back cleanly; the next loop
    iteration re-reads under FOR UPDATE, now sees the winner's committed
    row, and merges into it instead of losing the fill or leaving two OPEN
    lifecycles for the same symbol.
    """
    decision_stop, decision_target = await _resolve_decision_stop_target(
        session, fill.request_id
    )
    for attempt in range(1, _MAX_OPEN_INSERT_ATTEMPTS + 1):
        lifecycle = await get_open_lifecycle(session, symbol, for_update=True)

        if lifecycle is None:
            candidate = PositionLifecycle(
                symbol=symbol,
                status="OPEN",
                opened_at=fill.filled_at,
                entry_request_id=fill.request_id,
                entry_order_id=fill.order_id,
                current_qty=fill.fill_qty,
                average_entry_price=fill.fill_price,
                gross_buy_value_tl=fill.gross_value_tl,
                total_buy_cost_tl=fill.total_cost_tl,
                initial_stop_loss=decision_stop,
                active_stop_loss=decision_stop,
                initial_target_price=decision_target,
                active_target_price=decision_target,
                strategy_version=STRATEGY_VERSION,
                prompt_version=PROMPT_VERSION,
                config_hash=row.config_version,
                profile_code=row.profile_code,
            )
            try:
                async with session.begin_nested():
                    session.add(candidate)
                    await session.flush()
            except IntegrityError:
                logger.warning(
                    "LIFECYCLE_OPEN_INSERT_CONFLICT symbol=%s attempt=%s - "
                    "another fill opened it first, retrying as a merge",
                    symbol,
                    attempt,
                )
                session.expunge(candidate)
                continue
            if decision_stop is not None:
                await _record_stop_event(
                    session,
                    candidate,
                    old_stop=None,
                    new_stop=decision_stop,
                    event_type="INITIAL_STOP_CREATED",
                    source_request_id=fill.request_id,
                    source_order_id=fill.order_id,
                    reason="First BUY fill opened the position",
                )
            await session.flush()
            return candidate

        old_qty = lifecycle.current_qty or Decimal("0")
        old_avg = lifecycle.average_entry_price or Decimal("0")
        new_qty = old_qty + fill.fill_qty
        new_avg = (
            ((old_qty * old_avg) + (fill.fill_qty * fill.fill_price)) / new_qty
            if new_qty > 0
            else old_avg
        )
        lifecycle.current_qty = new_qty
        lifecycle.average_entry_price = new_avg
        lifecycle.gross_buy_value_tl = (lifecycle.gross_buy_value_tl or Decimal("0")) + (
            fill.gross_value_tl
        )
        lifecycle.total_buy_cost_tl = (lifecycle.total_buy_cost_tl or Decimal("0")) + (
            fill.total_cost_tl
        )

        old_stop = lifecycle.active_stop_loss
        if decision_stop is None:
            await _record_stop_event(
                session,
                lifecycle,
                old_stop=old_stop,
                new_stop=None,
                event_type="STOP_UPDATE_REJECTED",
                source_request_id=fill.request_id,
                source_order_id=fill.order_id,
                reason="New decision had no valid stop_loss; existing stop kept",
            )
        elif old_stop is None:
            lifecycle.active_stop_loss = decision_stop
            await _record_stop_event(
                session,
                lifecycle,
                old_stop=None,
                new_stop=decision_stop,
                event_type="INITIAL_STOP_CREATED",
                source_request_id=fill.request_id,
                source_order_id=fill.order_id,
                reason="First valid stop bound to an already-open position",
            )
        else:
            new_stop = max(old_stop, decision_stop)
            if new_stop > old_stop:
                lifecycle.active_stop_loss = new_stop
                await _record_stop_event(
                    session,
                    lifecycle,
                    old_stop=old_stop,
                    new_stop=new_stop,
                    event_type="STOP_TIGHTENED",
                    source_request_id=fill.request_id,
                    source_order_id=fill.order_id,
                    reason="Additional BUY fill tightened the active stop",
                )

        # A new target never silently overrides the existing one - only
        # fills the gap if the lifecycle had none yet (Task 4.2).
        if lifecycle.active_target_price is None and decision_target is not None:
            lifecycle.active_target_price = decision_target
            if lifecycle.initial_target_price is None:
                lifecycle.initial_target_price = decision_target

        await session.flush()
        return lifecycle

    raise RuntimeError(
        f"Could not resolve open-lifecycle insert conflict for {symbol} after "
        f"{_MAX_OPEN_INSERT_ATTEMPTS} attempts"
    )


async def _apply_sell_fill(
    session: AsyncSession,
    fill: OrderFill,
    symbol: str,
    lifecycle: PositionLifecycle | None,
) -> PositionLifecycle | None:
    if lifecycle is None:
        logger.warning(
            "SELL_FILL_NO_OPEN_LIFECYCLE symbol=%s request_id=%s", symbol, fill.request_id
        )
        return None

    sold_qty = fill.fill_qty
    if lifecycle.current_qty is not None and sold_qty > lifecycle.current_qty:
        sold_qty = lifecycle.current_qty
    if sold_qty <= 0:
        return lifecycle

    avg_entry = lifecycle.average_entry_price or Decimal("0")
    gross_realized = sold_qty * (fill.fill_price - avg_entry)
    remaining_qty = lifecycle.current_qty or Decimal("0")

    # total_buy_cost_tl is a cumulative running SUM of every BUY fill's cost
    # for this lifecycle - it is never decremented (it is reported as-is in
    # "toplam maliyet"). Each sale's cost share is instead derived from the
    # stable per-share buy cost ratio (total_buy_cost_tl * average_entry /
    # gross_buy_value_tl), so repeated partial sells allocate the same total
    # buy cost exactly once in aggregate, without ever mutating the field.
    total_buy_cost = lifecycle.total_buy_cost_tl or Decimal("0")
    gross_buy_value = lifecycle.gross_buy_value_tl or Decimal("0")
    if avg_entry > 0 and gross_buy_value > 0:
        buy_cost_per_share = (total_buy_cost * avg_entry) / gross_buy_value
    else:
        buy_cost_per_share = Decimal("0")
    buy_cost_share = sold_qty * buy_cost_per_share
    net_realized = gross_realized - buy_cost_share - fill.total_cost_tl

    lifecycle.gross_realized_pnl_tl = (lifecycle.gross_realized_pnl_tl or Decimal("0")) + (
        gross_realized
    )
    lifecycle.net_realized_pnl_tl = (lifecycle.net_realized_pnl_tl or Decimal("0")) + (
        net_realized
    )
    lifecycle.gross_sell_value_tl = (lifecycle.gross_sell_value_tl or Decimal("0")) + (
        fill.gross_value_tl
    )
    lifecycle.total_sell_cost_tl = (lifecycle.total_sell_cost_tl or Decimal("0")) + (
        fill.total_cost_tl
    )
    lifecycle.current_qty = remaining_qty - sold_qty

    if lifecycle.current_qty <= 0:
        lifecycle.current_qty = Decimal("0")
        lifecycle.status = "CLOSED"
        lifecycle.closed_at = fill.filled_at
        await _record_stop_event(
            session,
            lifecycle,
            old_stop=lifecycle.active_stop_loss,
            new_stop=None,
            event_type="POSITION_CLOSED",
            source_request_id=fill.request_id,
            source_order_id=fill.order_id,
            reason="SELL fill closed the remaining position",
        )
    else:
        await _record_stop_event(
            session,
            lifecycle,
            old_stop=lifecycle.active_stop_loss,
            new_stop=lifecycle.active_stop_loss,
            event_type="POSITION_PARTIALLY_CLOSED",
            source_request_id=fill.request_id,
            source_order_id=fill.order_id,
            reason=f"Partial SELL sold_qty={sold_qty}",
        )

    await session.flush()
    return lifecycle
