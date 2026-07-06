"""Signal evaluation endpoint — protected by Bearer token."""

from fastapi import APIRouter, Depends

from app.core.auth import verify_token
from app.models.signal import SignalRequest, SignalResponse

router = APIRouter(tags=["Signal"], dependencies=[Depends(verify_token)])


@router.post("/signal/evaluate")
async def evaluate_signal(body: SignalRequest) -> SignalResponse:
    """Evaluate a trading signal. (Stub — AI logic not yet implemented.)"""
    return SignalResponse(
        requestId=body.request_id,
        symbol=body.symbol,
        action="WAIT",
        qty=0.0,
        orderType="NONE",
        price=None,
        confidenceScore=0.0,
        riskScore=0.0,
        allowOrder=False,
        reason=f"AI evaluation not yet implemented — received signal for {body.symbol}",
        entryRange=None,
        stopLoss=None,
        targetPrice=None,
    )
