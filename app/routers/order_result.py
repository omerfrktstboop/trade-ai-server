"""Order result endpoint — records filled orders from Matriks IQ."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import verify_gateway_token
from app.db.session import async_session_factory
from app.models.db import OrderLog
from sqlalchemy import select
from app.services.notifications import notify_order_event
from app.services.order_state_machine import FINAL, RANK, transition
from app.services.order_lifecycle import apply_callback
from app.services.decision_gate import decision_cache

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Order"], dependencies=[Depends(verify_gateway_token)])


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
    """ACK is issued only after the database transaction commits."""

    status: str
    persisted: bool
    request_id: str = Field(alias="requestId")
    applied_status: str | None = Field(None, alias="appliedStatus")

    model_config = {"populate_by_name": True}


FINAL_STATUSES = FINAL
STATUS_RANK = RANK


def should_apply_status(current: str | None, incoming: str) -> bool:
    """Return whether an exchange event may advance the persisted lifecycle."""
    return transition(current, incoming)[0]


# ── Endpoint ─────────────────────────────────────────────────────────────────


@router.post("/order-result")
async def record_order_result(body: OrderResultRequest) -> OrderResultResponse:
    """Record a completed order result from the trading platform.

    Persists to ``order_logs`` table and returns ``{"status": "ok"}``.
    DB errors are swallowed so that the endpoint never blocks Matriks IQ.
    """
    event_applied = False
    persisted = False
    applied_status: str | None = None
    try:
        async with async_session_factory() as session:
            incoming_status = body.status.upper()
            order_qty = body.order_qty if body.order_qty is not None else body.qty
            filled_qty = body.filled_qty if body.filled_qty is not None else body.qty
            entry, event_applied = await apply_callback(session, request_id=body.request_id, symbol=body.symbol, action=body.action, status=incoming_status, order_qty=order_qty or 0.0, filled_qty=filled_qty or 0.0, last_fill_qty=body.last_fill_qty or 0.0, avg_price=body.avg_price or body.price, limit_price=body.limit_price, order_id=body.order_id, message=body.matriks_message)
            persisted = True
            applied_status = entry.status
    except Exception as exc:
        logger.exception(
            "Failed to persist order result request_id=%s symbol=%s",
            body.request_id,
            body.symbol,
        )
        raise HTTPException(status_code=503, detail="order result persistence unavailable") from exc

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
    if event_applied and body.status.upper() in {"PARTIALLY_FILLED", "FILLED"}:
        decision_cache.clear(body.symbol)

    return OrderResultResponse(
        status="ok" if persisted else "error",
        persisted=persisted,
        requestId=body.request_id,
        appliedStatus=applied_status,
    )
