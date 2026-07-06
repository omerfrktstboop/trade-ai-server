"""Signal evaluation endpoint — protected by Bearer token."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import verify_token

router = APIRouter(tags=["Signal"], dependencies=[Depends(verify_token)])


class EvaluateRequest(BaseModel):
    """Placeholder schema for signal evaluation requests."""

    symbol: str
    signal: str


class EvaluateResponse(BaseModel):
    """Placeholder response."""

    status: str
    detail: str


@router.post("/signal/evaluate")
async def evaluate_signal(body: EvaluateRequest) -> EvaluateResponse:
    """Evaluate a trading signal. (Stub — AI logic not yet implemented.)"""
    return EvaluateResponse(
        status="received",
        detail=f"Signal '{body.signal}' for {body.symbol} queued for evaluation",
    )
