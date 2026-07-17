"""Authoritative persistent order ledger.

This service deliberately separates *reservation*, *send started*, and the
gateway result.  An uncertain transport result is never retried as an order.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import OrderLog


FINAL_STATES = {"FILLED", "REJECTED", "CANCELED", "CANCELLED", "EXPIRED"}
PENDING_STATES = {
    "RESERVED",
    "SEND_IN_PROGRESS",
    "SENT_PENDING",
    "SEND_UNKNOWN",
    "NEW",
    "PARTIALLY_FILLED",
    "CANCEL_REQUESTED",
}


def fingerprint(
    *,
    symbol: str,
    side: str,
    qty: float,
    limit_price: float,
    mode: str,
    order_type: str = "LIMIT",
) -> tuple[str, float]:
    """Return stable request fingerprint and the ledger's rounded price."""
    rounded = Decimal(str(limit_price)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    payload = "|".join(
        (
            symbol.strip().upper(),
            side.strip().upper(),
            str(int(qty)),
            format(rounded, "f"),
            mode.strip().upper(),
            order_type.strip().upper(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), float(rounded)


async def reserve_order(
    session: AsyncSession,
    *,
    request_id: str,
    symbol: str,
    side: str,
    qty: float,
    limit_price: float,
    mode: str,
    order_type: str = "LIMIT",
    config_version: str | None = None,
    profile_code: str | None = None,
    account_ref: str | None = None,
    commit: bool = True,
) -> tuple[OrderLog, bool, str | None]:
    """Atomically reserve an order; return (row, may_send, rejection).

    ``account_ref`` (verilirse) rezervasyonla AYNI transaction'da OrderLog'a
    yazılır — fill'ler bu sabit hesap referansını kullanır (Fix #1, atomik).
    """
    request_id = request_id.strip()
    if not request_id:
        raise ValueError("request_id is required")
    if not math.isfinite(qty) or qty <= 0 or not float(qty).is_integer():
        raise ValueError("qty must be a positive integer")
    if not math.isfinite(limit_price) or limit_price <= 0:
        raise ValueError("limit_price must be finite and positive")
    normalized_symbol = symbol.strip().upper()
    normalized_side = side.strip().upper()
    normalized_mode = mode.strip().upper()
    normalized_order_type = order_type.strip().upper()
    fp, rounded_price = fingerprint(
        symbol=normalized_symbol,
        side=normalized_side,
        qty=qty,
        limit_price=limit_price,
        mode=normalized_mode,
        order_type=normalized_order_type,
    )
    row = (
        await session.execute(select(OrderLog).where(OrderLog.request_id == request_id))
    ).scalar_one_or_none()
    if row is not None:
        if row.request_fingerprint and row.request_fingerprint != fp:
            return row, False, "requestId fingerprint mismatch"
        return row, False, None
    pending = (
        await session.execute(
            select(OrderLog).where(
                OrderLog.symbol == normalized_symbol,
                OrderLog.action == normalized_side,
                OrderLog.status.in_(PENDING_STATES),
            )
        )
    ).scalar_one_or_none()
    if pending is not None:
        return pending, False, "pending symbol+side order exists"
    now = datetime.now(timezone.utc)
    values = dict(
        request_id=request_id,
        request_fingerprint=fp,
        symbol=normalized_symbol,
        action=normalized_side,
        qty=float(qty),
        price=float(limit_price),
        order_qty=float(qty),
        limit_price=float(limit_price),
        rounded_limit_price=rounded_price,
        status="RESERVED",
        state="RESERVED",
        mode=normalized_mode,
        order_type=normalized_order_type,
        reservation_created_at=now,
        config_version=config_version,
        profile_code=profile_code,
        account_ref=(account_ref or None),
    )
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(OrderLog).values(**values)
    elif dialect == "sqlite":
        statement = sqlite_insert(OrderLog).values(**values)
    else:
        raise RuntimeError(f"Unsupported order ledger dialect: {dialect}")
    statement = statement.on_conflict_do_nothing(
        index_elements=[OrderLog.request_id]
    ).returning(OrderLog.id)
    inserted_id = (await session.execute(statement)).scalar_one_or_none()
    if commit:
        await session.commit()
    else:
        await session.flush()
    row = (
        await session.execute(select(OrderLog).where(OrderLog.request_id == request_id))
    ).scalar_one()
    if inserted_id is None:
        if row.request_fingerprint != fp:
            return row, False, "requestId fingerprint mismatch"
        return row, False, None
    return row, True, None


async def mark_send_started(session: AsyncSession, row: OrderLog) -> None:
    row.status = "SEND_IN_PROGRESS"
    row.state = "SEND_IN_PROGRESS"
    row.send_started_at = datetime.now(timezone.utc)
    from app.services.cash_reservation import sync_cash_reservation

    await sync_cash_reservation(session, row)
    await session.commit()


async def mark_send_result(
    session: AsyncSession,
    row: OrderLog,
    *,
    status: str,
    message: str,
    uncertain: bool = False,
) -> None:
    state = "SEND_UNKNOWN" if uncertain else status.upper()
    row.status = state
    row.state = state
    row.matrix_message = message
    row.error_message = message if uncertain else None
    if uncertain:
        row.error_code = "SEND_UNKNOWN"
    elif state == "SENT_PENDING":
        row.sent_at = datetime.now(timezone.utc)
    elif state in FINAL_STATES:
        row.finalized_at = datetime.now(timezone.utc)
    from app.services.cash_reservation import sync_cash_reservation

    await sync_cash_reservation(session, row)
    await session.commit()
