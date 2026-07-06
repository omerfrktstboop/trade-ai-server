"""Signal evaluation endpoint — protected by Bearer token.

Flow::

    SignalRequest  →  DummyStrategy  →  RiskEngine  →  SignalResponse  →  log
"""

from fastapi import APIRouter, Depends

from app.core.auth import verify_token
from app.core.logger import log_signal_evaluation
from app.core.risk_config import risk_config
from app.models.signal import SignalRequest, SignalResponse
from app.services.risk_engine import RiskEngine
from app.services.strategy import generate_dummy_decision

router = APIRouter(tags=["Signal"], dependencies=[Depends(verify_token)])

# ── Engine singleton ──────────────────────────────────────────────────────────

_risk_engine = RiskEngine(risk_config)


@router.post("/signal/evaluate")
async def evaluate_signal(body: SignalRequest) -> SignalResponse:
    """Evaluate a trading signal end-to-end.

    1. Parse ``SignalRequest`` from JSON body.
    2. Run **dummy strategy** to produce a raw ``RiskDecision``.
    3. Pass through ``RiskEngine`` for safety overrides.
    4. Return the final, safety-checked ``SignalResponse``.
    5. Log the request/response pair to ``logs/signal.log``.
    """
    # ── 1. Generate raw decision ─────────────────────────────────────────
    decision = generate_dummy_decision(body, risk_config)

    # ── 2. Apply risk engine (final safety net) ──────────────────────────
    response = _risk_engine.evaluate(body, decision)

    # ── 3. Persist to JSON-lines log ─────────────────────────────────────
    log_signal_evaluation(
        request_id=body.request_id,
        symbol=body.symbol,
        mode=body.mode.value,
        request=body.model_dump(by_alias=True, exclude={"mode"}),
        response=response.model_dump(by_alias=True),
    )

    return response
