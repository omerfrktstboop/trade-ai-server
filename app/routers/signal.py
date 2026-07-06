"""Signal evaluation endpoint — protected by Bearer token."""

from fastapi import APIRouter, Depends

from app.core.auth import verify_token
from app.core.logger import log_signal_evaluation
from app.models.signal import SignalMode, SignalRequest, SignalResponse

router = APIRouter(tags=["Signal"], dependencies=[Depends(verify_token)])


@router.post("/signal/evaluate")
async def evaluate_signal(body: SignalRequest) -> SignalResponse:
    """Evaluate a trading signal.

    Currently returns safe-default responses. AI evaluation will be wired
    into this endpoint in a future iteration.
    """
    # Guard: PAPER mode never places real orders
    allow_order = False

    response = SignalResponse(
        requestId=body.request_id,
        symbol=body.symbol,
        action="WAIT",
        qty=0.0,
        orderType="NONE",
        price=None,
        confidenceScore=0.0,
        riskScore=0.0,
        allowOrder=allow_order,
        reason="Safe default: PAPER mode or no decision.",
        entryRange=None,
        stopLoss=None,
        targetPrice=None,
    )

    # Log to JSON-lines file (no sensitive data)
    log_signal_evaluation(
        request_id=body.request_id,
        symbol=body.symbol,
        mode=body.mode.value,
        request=body.model_dump(by_alias=True, exclude={"mode"}),
        response=response.model_dump(by_alias=True),
    )

    return response
