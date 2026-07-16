"""Concurrent-safe callback persistence for the authoritative order row."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import OrderLog
from app.services.fill_ledger import record_fill_delta
from app.services.measurement_repair import enqueue_repair_job
from app.services.order_state_machine import FINAL, transition
from app.services.position_lifecycle_engine import (
    LifecycleIntegrityError,
    apply_fill_to_lifecycle,
)

logger = logging.getLogger(__name__)


async def apply_callback(
    session: AsyncSession,
    *,
    request_id: str,
    symbol: str,
    action: str,
    status: str,
    order_qty: float,
    filled_qty: float,
    last_fill_qty: float,
    avg_price: float | None,
    limit_price: float | None,
    order_id: str | None,
    message: str,
) -> tuple[OrderLog, bool]:
    values = dict(
        request_id=request_id,
        symbol=symbol.upper(),
        action=action.upper(),
        qty=order_qty,
        price=limit_price,
        order_qty=order_qty,
        limit_price=limit_price,
        rounded_limit_price=limit_price,
        order_type="LIMIT",
        status="RESERVED",
        state="RESERVED",
        filled_qty=0.0,
        last_fill_qty=0.0,
        matrix_message=message,
    )
    dialect = session.bind.dialect.name
    statement = (
        (pg_insert(OrderLog) if dialect == "postgresql" else sqlite_insert(OrderLog))
        .values(**values)
        .on_conflict_do_nothing(index_elements=["request_id"])
    )
    await session.execute(statement)
    row = (
        await session.execute(
            select(OrderLog).where(OrderLog.request_id == request_id).with_for_update()
        )
    ).scalar_one()
    allowed, _ = transition(
        row.status,
        status,
        current_filled=row.filled_qty or 0.0,
        incoming_filled=filled_qty,
    )
    if not allowed:
        await session.commit()
        return row, False
    changed = row.status != status.upper() or filled_qty > (row.filled_qty or 0.0)
    old_filled_qty = row.filled_qty or 0.0
    old_avg_price = row.avg_price
    row.status = status.upper()
    row.state = status.upper()
    row.qty = max(row.qty or 0.0, order_qty)
    row.order_qty = max(row.order_qty or 0.0, order_qty)
    row.filled_qty = max(row.filled_qty or 0.0, filled_qty)
    row.last_fill_qty = last_fill_qty
    row.avg_price = avg_price or row.avg_price
    row.price = avg_price or limit_price or row.price
    row.limit_price = limit_price or row.limit_price
    row.rounded_limit_price = limit_price or row.rounded_limit_price
    row.order_id = order_id or row.order_id
    row.matrix_message = message
    if row.status in FINAL:
        row.finalized_at = datetime.now(timezone.utc)
    from app.services.cash_reservation import sync_cash_reservation

    await sync_cash_reservation(session, row, strict=False)

    if changed:
        try:
            async with session.begin_nested():
                fill = await record_fill_delta(
                    session,
                    row,
                    old_filled_qty=old_filled_qty,
                    old_avg_price=old_avg_price,
                    new_filled_qty=row.filled_qty,
                    new_avg_price=row.avg_price,
                    limit_price=row.limit_price,
                    order_id=row.order_id,
                    filled_at=datetime.now(timezone.utc),
                )
                if fill is not None:
                    await apply_fill_to_lifecycle(session, row, fill)
        except Exception as exc:
            # The fill ledger/lifecycle is measurement-only - a SAVEPOINT
            # isolates it so a failure here never blocks or corrupts the
            # authoritative OrderLog callback from committing. Without a
            # repair job, the next callback for the same order reports the
            # same cumulative filled_qty and computes delta_qty=0, so this
            # exact gap could never be recovered from a future callback.
            logger.exception(
                "FILL_LEDGER_UPDATE_FAILED request_id=%s", row.request_id
            )
            repair_type = (
                "LIFECYCLE_RECONCILIATION"
                if isinstance(exc, LifecycleIntegrityError)
                else "FILL_RECONCILIATION"
            )
            try:
                await enqueue_repair_job(
                    session,
                    repair_type=repair_type,
                    last_error=repr(exc),
                    request_id=row.request_id,
                    order_log_id=row.id,
                    symbol=row.symbol,
                )
            except Exception:
                logger.exception(
                    "MEASUREMENT_REPAIR_JOB_ENQUEUE_FAILED request_id=%s",
                    row.request_id,
                )

    await session.commit()
    return row, changed
