"""Order result endpoint — records filled orders from Matriks IQ."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.auth import verify_token
from app.db.session import async_session_factory
from app.models.db import OrderLog

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Order"], dependencies=[Depends(verify_token)])


# ── Schema ───────────────────────────────────────────────────────────────────


class OrderResultRequest(BaseModel):
    """Payload from Matriks IQ when an order is executed."""

    request_id: str = Field(..., alias="requestId")
    symbol: str
    action: str
    qty: float
    price: float
    status: str
    matriks_message: str = Field(..., alias="matriksMessage")
    order_id: str | None = Field(None, alias="orderId")

    model_config = {"populate_by_name": True}


class OrderResultResponse(BaseModel):
    """Simple acknowledgement."""

    status: str


# ── Endpoint ─────────────────────────────────────────────────────────────────


@router.post("/order-result")
async def record_order_result(body: OrderResultRequest) -> OrderResultResponse:
    """Record a completed order result from the trading platform.

    Persists to ``order_logs`` table and returns ``{"status": "ok"}``.
    DB errors are swallowed so that the endpoint never blocks Matriks IQ.
    """
    try:
        async with async_session_factory() as session:
            entry = OrderLog(
                request_id=body.request_id,
                symbol=body.symbol,
                action=body.action,
                qty=body.qty,
                price=body.price,
                status=body.status,
                order_id=body.order_id,
                matrix_message=body.matriks_message,
            )
            session.add(entry)
            await session.commit()
    except Exception:
        logger.exception(
            "Failed to persist order result request_id=%s symbol=%s",
            body.request_id,
            body.symbol,
        )

    return OrderResultResponse(status="ok")
