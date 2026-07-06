"""Signal evaluation endpoint — protected by Bearer token.

Flow::

    SignalRequest  →  AiProvider.decide()  →  RiskEngine  →  SignalResponse  →  log
"""

from fastapi import APIRouter, Depends

from app.core.auth import verify_token
from app.core.logger import log_signal_evaluation
from app.core.risk_config import risk_config
from app.models.signal import SignalAction, SignalRequest, SignalResponse
from app.services.ai_provider import get_default_provider
from app.services.risk_engine import RiskDecision, RiskEngine

router = APIRouter(tags=["Signal"], dependencies=[Depends(verify_token)])

# ── Engine singleton ──────────────────────────────────────────────────────────

_risk_engine = RiskEngine(risk_config)
_provider = get_default_provider()


@router.post("/signal/evaluate")
async def evaluate_signal(body: SignalRequest) -> SignalResponse:
    """Evaluate a trading signal end-to-end.

    1. Parse ``SignalRequest`` from JSON body.
    2. Serialize to a dict and ask the **AI provider** for a raw decision.
    3. Convert the provider's dict into a ``RiskDecision``.
    4. Pass through ``RiskEngine`` for safety overrides.
    5. Return the final, safety-checked ``SignalResponse``.
    6. Log the request/response pair to ``logs/signal.log``.
    """
    # ── 1. Build payload for the AI provider ──────────────────────────────
    payload = _build_payload(body)

    # ── 2. Ask provider ───────────────────────────────────────────────────
    raw = await _provider.decide(payload)

    # ── 3. Wire into RiskDecision ─────────────────────────────────────────
    decision = _dict_to_risk_decision(raw, body)

    # ── 4. Apply risk engine ──────────────────────────────────────────────
    response = _risk_engine.evaluate(body, decision)

    # ── 5. Persist to JSON-lines log ──────────────────────────────────────
    log_signal_evaluation(
        request_id=body.request_id,
        symbol=body.symbol,
        mode=body.mode.value,
        request=body.model_dump(by_alias=True, exclude={"mode"}),
        response=response.model_dump(by_alias=True),
    )

    return response


# ── Helpers ───────────────────────────────────────────────────────────────────


def _build_payload(req: SignalRequest) -> dict:
    """Convert a SignalRequest into a plain dict for the AI provider."""
    return {
        "symbol": req.symbol,
        "timeframe": req.timeframe,
        "lastPrice": req.last_price,
        "open": req.open,
        "high": req.high,
        "low": req.low,
        "volume": req.volume,
        "rsi": req.rsi,
        "ema20": req.ema20,
        "ema50": req.ema50,
        "macd": req.macd,
        "macdSignal": req.macd_signal,
        "botPositionQty": req.bot_position_qty,
        "totalAccountQty": req.total_account_qty,
        "lockedQty": req.locked_qty,
        "lockedLongTermQty": req.locked_long_term_qty,
    }


def _dict_to_risk_decision(raw: dict, _req: SignalRequest) -> RiskDecision:
    """Parse a provider response dict into a RiskDecision."""
    action = SignalAction(raw.get("action", "WAIT"))
    return RiskDecision(
        action=action,
        confidence=float(raw.get("confidence", 0)),
        risk_score=float(raw.get("risk_score", 0)),
        reason=str(raw.get("reason", "Provider returned no reason")),
        qty=float(raw.get("qty", 0)),
        entry_range=None,  # TODO: parse from raw when provider supports it
        stop_loss=raw.get("stop_loss"),
        target_price=raw.get("target_price"),
    )
