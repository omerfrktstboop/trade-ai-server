"""Lifecycle qty integrity check (Task 2.3): compares an OPEN
PositionLifecycle's current_qty against SUM(BUY fills) - SUM(SELL fills)
recorded since it opened. A mismatch is never silently overwritten - it is
either safely auto-corrected (only when the lifecycle was opened by a real
fill, so its fill history is trustworthy) or escalated to MANUAL_REVIEW.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import OrderFill, PositionLifecycle, PositionStopEvent
from app.services.measurement_repair import enqueue_repair_job

logger = logging.getLogger(__name__)

# A discrepancy this small is treated as Decimal/rounding noise, not a real
# data-integrity gap - avoids flapping repair jobs on sub-lot arithmetic.
_QTY_EPSILON = Decimal("0.0000000001")


async def compute_fill_derived_qty(
    session: AsyncSession, lifecycle: PositionLifecycle
) -> Decimal:
    """SUM(BUY fill_qty) - SUM(SELL fill_qty) for this lifecycle's symbol,
    scoped to fills at/after opened_at. An OPEN lifecycle has no closed_at
    upper bound by definition, so every fill since it opened belongs to it -
    a prior, already-closed lifecycle for the same symbol never overlaps
    this window."""
    row = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(case((OrderFill.action == "BUY", OrderFill.fill_qty), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(case((OrderFill.action == "SELL", OrderFill.fill_qty), else_=0)),
                    0,
                ),
            ).where(
                OrderFill.symbol == lifecycle.symbol,
                OrderFill.filled_at >= lifecycle.opened_at,
            )
        )
    ).one()
    bought = Decimal(str(row[0] or 0))
    sold = Decimal(str(row[1] or 0))
    return bought - sold


async def check_lifecycle_qty_integrity(
    session: AsyncSession, lifecycle: PositionLifecycle
) -> Decimal | None:
    """Return the discrepancy (fill-derived - recorded), or None if they
    match within rounding noise."""
    fill_derived_qty = await compute_fill_derived_qty(session, lifecycle)
    recorded_qty = lifecycle.current_qty or Decimal("0")
    discrepancy = fill_derived_qty - recorded_qty
    if abs(discrepancy) <= _QTY_EPSILON:
        return None
    return discrepancy


async def reconcile_lifecycle_qty(
    session: AsyncSession, lifecycle: PositionLifecycle
) -> bool:
    """Attempt to resolve a qty mismatch for one OPEN lifecycle.

    Returns True if resolved (matched already, or safely auto-corrected),
    False if it needed to be escalated to MANUAL_REVIEW instead. Never
    guesses: a lifecycle without a real entry_request_id (i.e. seeded by the
    legacy BotPosition backfill) has no trustworthy fill history to
    reconstruct current_qty from, so it is always escalated rather than
    auto-corrected.
    """
    discrepancy = await check_lifecycle_qty_integrity(session, lifecycle)
    if discrepancy is None:
        return True

    if lifecycle.entry_request_id is None:
        logger.error(
            "LIFECYCLE_QTY_MISMATCH_UNTRUSTED_HISTORY symbol=%s lifecycleId=%s "
            "recordedQty=%s discrepancy=%s",
            lifecycle.symbol,
            lifecycle.id,
            lifecycle.current_qty,
            discrepancy,
        )
        await enqueue_repair_job(
            session,
            repair_type="LIFECYCLE_RECONCILIATION",
            last_error=(
                f"qty mismatch on a backfilled lifecycle (no real fill history): "
                f"recorded={lifecycle.current_qty} discrepancy={discrepancy}"
            ),
            symbol=lifecycle.symbol,
        )
        return False

    old_qty = lifecycle.current_qty
    corrected_qty = (old_qty or Decimal("0")) + discrepancy
    if corrected_qty < 0:
        logger.error(
            "LIFECYCLE_QTY_MISMATCH_NEGATIVE_CORRECTION symbol=%s lifecycleId=%s "
            "recordedQty=%s discrepancy=%s",
            lifecycle.symbol,
            lifecycle.id,
            old_qty,
            discrepancy,
        )
        await enqueue_repair_job(
            session,
            repair_type="LIFECYCLE_RECONCILIATION",
            last_error=(
                f"fill-derived correction would go negative: recorded={old_qty} "
                f"discrepancy={discrepancy}"
            ),
            symbol=lifecycle.symbol,
        )
        return False

    logger.warning(
        "LIFECYCLE_QTY_RECONCILED symbol=%s lifecycleId=%s oldQty=%s newQty=%s",
        lifecycle.symbol,
        lifecycle.id,
        old_qty,
        corrected_qty,
    )
    lifecycle.current_qty = corrected_qty
    session.add(
        PositionStopEvent(
            position_lifecycle_id=lifecycle.id,
            symbol=lifecycle.symbol,
            old_stop=lifecycle.active_stop_loss,
            new_stop=lifecycle.active_stop_loss,
            event_type="QTY_RECONCILED",
            source_request_id=None,
            source_order_id=None,
            reason=f"current_qty corrected from {old_qty} to {corrected_qty} (fill-derived)",
        )
    )
    await session.flush()
    return True


async def reconcile_all_open_lifecycles(session: AsyncSession) -> tuple[int, int]:
    """Check every OPEN lifecycle; returns (checked_count, corrected_count)."""
    lifecycles = (
        (
            await session.execute(
                select(PositionLifecycle).where(PositionLifecycle.status == "OPEN")
            )
        )
        .scalars()
        .all()
    )
    corrected = 0
    for lifecycle in lifecycles:
        discrepancy = await check_lifecycle_qty_integrity(session, lifecycle)
        if discrepancy is None:
            continue
        resolved = await reconcile_lifecycle_qty(session, lifecycle)
        if resolved:
            corrected += 1
    await session.commit()
    return len(lifecycles), corrected
