"""Order result endpoint — protected by Bearer token."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import verify_token

router = APIRouter(tags=["Order"], dependencies=[Depends(verify_token)])


class OrderResultRequest(BaseModel):
    """Placeholder schema for order result reporting."""

    order_id: str
    symbol: str
    side: str
    filled: bool
    price: float | None = None


class OrderResultResponse(BaseModel):
    """Placeholder response."""

    status: str
    order_id: str


@router.post("/order-result")
async def record_order_result(body: OrderResultRequest) -> OrderResultResponse:
    """Record a completed order result. (Stub — DB logic not yet implemented.)"""
    return OrderResultResponse(
        status="recorded",
        order_id=body.order_id,
    )
