"""Concurrent-safe callback persistence for the authoritative order row."""

from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import OrderLog
from app.services.order_state_machine import FINAL, transition


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
    await session.commit()
    return row, changed
