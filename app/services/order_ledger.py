"""Authoritative persistent order ledger.

This service deliberately separates *reservation*, *send started*, and the
gateway result.  An uncertain transport result is never retried as an order.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import OrderLog


FINAL_STATES = {"FILLED", "REJECTED", "CANCELED", "CANCELLED", "EXPIRED"}
PENDING_STATES = {"RESERVED", "SEND_IN_PROGRESS", "SENT_PENDING", "SEND_UNKNOWN", "NEW", "PARTIALLY_FILLED", "CANCEL_REQUESTED"}


def fingerprint(*, symbol: str, side: str, qty: float, limit_price: float, mode: str, order_type: str = "LIMIT") -> tuple[str, float]:
    """Return stable request fingerprint and the ledger's rounded price."""
    rounded = Decimal(str(limit_price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    payload = "|".join((symbol.strip().upper(), side.strip().upper(), str(int(qty)), format(rounded, "f"), mode.strip().upper(), order_type.strip().upper()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), float(rounded)


async def reserve_order(session: AsyncSession, *, request_id: str, symbol: str, side: str, qty: float, limit_price: float, mode: str, order_type: str = "LIMIT", config_version: str | None = None, profile_code: str | None = None) -> tuple[OrderLog, bool, str | None]:
    """Atomically reserve an order; return (row, may_send, rejection)."""
    request_id = request_id.strip()
    fp, rounded_price = fingerprint(symbol=symbol, side=side, qty=qty, limit_price=limit_price, mode=mode, order_type=order_type)
    row = (await session.execute(select(OrderLog).where(OrderLog.request_id == request_id))).scalar_one_or_none()
    if row is not None:
        if row.request_fingerprint and row.request_fingerprint != fp:
            return row, False, "requestId fingerprint mismatch"
        return row, False, None
    now = datetime.now(timezone.utc)
    row = OrderLog(request_id=request_id, request_fingerprint=fp, symbol=symbol.strip().upper(), action=side.strip().upper(), qty=float(qty), price=float(limit_price), rounded_limit_price=rounded_price, status="RESERVED", mode=mode.strip().upper(), order_type=order_type.strip().upper(), reservation_created_at=now, config_version=config_version, profile_code=profile_code)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row, True, None


async def mark_send_started(session: AsyncSession, row: OrderLog) -> None:
    row.status = "SEND_IN_PROGRESS"
    row.send_started_at = datetime.now(timezone.utc)
    await session.commit()


async def mark_send_result(session: AsyncSession, row: OrderLog, *, status: str, message: str, uncertain: bool = False) -> None:
    state = "SEND_UNKNOWN" if uncertain else status.upper()
    row.status = state
    row.matrix_message = message
    if uncertain:
        row.error_code = "SEND_UNKNOWN"
    elif state == "SENT_PENDING":
        row.sent_at = datetime.now(timezone.utc)
    elif state in FINAL_STATES:
        row.finalized_at = datetime.now(timezone.utc)
    await session.commit()
