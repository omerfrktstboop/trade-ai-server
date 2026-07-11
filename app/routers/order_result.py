"""Order result endpoint — records filled orders from Matriks IQ."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.auth import verify_token
from app.db.session import async_session_factory
from app.models.db import OrderLog
from sqlalchemy import select
from app.services.notifications import notify_order_event

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Order"], dependencies=[Depends(verify_token)])


# ── Schema ───────────────────────────────────────────────────────────────────


class OrderResultRequest(BaseModel):
    """Payload from Matriks IQ when an order is executed."""

    request_id: str = Field(..., alias="requestId")
    symbol: str
    action: str
    qty: float | None = None  # legacy: cumulative filled qty when supplied by gateway
    price: float | None = None  # legacy: average execution/limit price
    order_qty: float | None = Field(None, alias="orderQty")
    filled_qty: float | None = Field(None, alias="filledQty")
    last_fill_qty: float | None = Field(None, alias="lastFillQty")
    avg_price: float | None = Field(None, alias="avgPrice")
    limit_price: float | None = Field(None, alias="limitPrice")
    status: str
    matriks_message: str = Field(..., alias="matriksMessage")
    order_id: str | None = Field(None, alias="orderId")

    model_config = {"populate_by_name": True}


class OrderResultResponse(BaseModel):
    """Simple acknowledgement."""

    status: str


FINAL_STATUSES = {"FILLED", "REJECTED", "CANCELED", "CANCELLED", "EXPIRED", "ERROR"}
STATUS_RANK = {
    "PENDING": 0,
    "SENT_PENDING": 10,
    "NEW": 20,
    "SENT": 25,
    "CANCEL_REQUESTED": 30,
    "PARTIALLY_FILLED": 40,
    "FILLED": 100,
    "REJECTED": 100,
    "CANCELED": 100,
    "CANCELLED": 100,
    "EXPIRED": 100,
    "ERROR": 100,
}


def should_apply_status(current: str | None, incoming: str) -> bool:
    """Return whether an exchange event may advance the persisted lifecycle."""
    old = (current or "PENDING").upper()
    new = incoming.upper()
    if old in FINAL_STATUSES:
        return old == new
    return STATUS_RANK.get(new, 0) >= STATUS_RANK.get(old, 0)


# ── Endpoint ─────────────────────────────────────────────────────────────────


@router.post("/order-result")
async def record_order_result(body: OrderResultRequest) -> OrderResultResponse:
    """Record a completed order result from the trading platform.

    Persists to ``order_logs`` table and returns ``{"status": "ok"}``.
    DB errors are swallowed so that the endpoint never blocks Matriks IQ.
    """
    event_applied = False
    try:
        async with async_session_factory() as session:
            entry = (
                await session.execute(
                    select(OrderLog)
                    .where(OrderLog.request_id == body.request_id)
                    .order_by(OrderLog.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            incoming_status = body.status.upper()
            order_qty = body.order_qty if body.order_qty is not None else body.qty
            filled_qty = body.filled_qty if body.filled_qty is not None else body.qty
            effective_qty = filled_qty if filled_qty is not None else (order_qty or 0.0)
            effective_price = body.avg_price or body.price or body.limit_price
            if entry is None:
                entry = OrderLog(request_id=body.request_id, symbol=body.symbol,
                    action=body.action, qty=effective_qty, price=effective_price,
                    status=incoming_status, order_id=body.order_id,
                    matrix_message=body.matriks_message)
                session.add(entry)
                event_applied = True
            elif should_apply_status(entry.status, incoming_status):
                event_applied = entry.status.upper() != incoming_status
                entry.status = incoming_status
                entry.qty = max(entry.qty or 0.0, effective_qty)
                entry.price = effective_price or entry.price
                entry.order_id = body.order_id or entry.order_id
                entry.matrix_message = body.matriks_message
            await session.commit()
    except Exception:
        logger.exception(
            "Failed to persist order result request_id=%s symbol=%s",
            body.request_id,
            body.symbol,
        )

    if event_applied and body.status.upper() in FINAL_STATUSES:
        await notify_order_event(
            body.status,
            symbol=body.symbol,
            side=body.action,
            qty=body.filled_qty if body.filled_qty is not None else (body.qty or 0.0),
            price=body.avg_price or body.price or body.limit_price,
            order_id=body.order_id,
            reason=body.matriks_message,
            request_id=body.request_id,
        )

    return OrderResultResponse(status="ok")
