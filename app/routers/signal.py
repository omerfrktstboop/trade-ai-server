"""Signal evaluation endpoint - protected by Bearer token.

Flow::

    SignalRequest  ->  AiProvider.decide()  ->  RiskEngine  ->  SignalResponse
                       |                         |                |
                   AiDecision               RiskDecision    market_snapshots

This is the single-shot, caller-supplies-the-data endpoint, kept for manual
testing and debugging. The live trading path no longer goes through HTTP at
all: ``app/services/scanner.py`` pulls market data from the Matriks gateway
and calls ``app/services/evaluator.py`` in-process.

Every pipeline helper below lives in the evaluator - this module is a thin
HTTP wrapper around it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.core.auth import verify_evaluation_token
from app.core.logger import log_signal_evaluation
from app.models.signal import SignalRequest, SignalResponse
from app.services.ai_provider import get_default_provider
from app.services.evaluator import (
    build_ai_decision_context,
    build_payload,
    dict_to_risk_decision,
    kill_switch_response,
    persist_evaluation,
    persist_sizing_audit,
    with_resolved_daily_trade_count,
    with_runtime_controls,
    with_trade_eligibility,
)
from app.services.fundamentals_service import get_fundamentals_context
from app.services.news_service import get_news_context

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Signal"], dependencies=[Depends(verify_evaluation_token)])

_provider = get_default_provider()


@router.post("/signal/evaluate")
async def evaluate_signal(body: SignalRequest) -> SignalResponse:
    """Evaluate a trading signal end-to-end.

    1. Parse ``SignalRequest`` from JSON body.
    2. Serialize to a dict and ask the **AI provider** for a raw decision.
    3. Convert the provider's dict into a ``RiskDecision``.
    4. Pass through ``RiskEngine`` for safety overrides.
    5. Log to ``logs/signal.log``.
    6. Persist to ``market_snapshots``, ``ai_decisions``, ``risk_decisions``.
    """
    (
        body,
        runtime_engine,
        kill_switch_enabled,
        _demo_allow_downtrend_buy,
    ) = await with_runtime_controls(body)
    # ``tradeEligible`` is server-authoritative.  Never trust the value sent by
    # a caller of this diagnostic endpoint; resolve the active, non-expired DB
    # watchlist row exactly as the in-process scanner does.
    body = await with_trade_eligibility(body)
    if kill_switch_enabled:
        response = kill_switch_response(body)
        raw = {
            "action": "WAIT",
            "confidence": 0.0,
            "risk_score": 0.0,
            "reason": response.reason,
        }
        payload = build_payload(body, active_config=runtime_engine.config)
        await log_signal_evaluation(
            request_id=body.request_id,
            symbol=body.symbol,
            request=body.model_dump(by_alias=True, mode="json"),
            response=response.model_dump(by_alias=True, mode="json"),
        )
        await persist_evaluation(body, payload, raw, response)
        return response

    # == 1. Fetch external context (news + admin-entered fundamentals -
    # fund/broker flow context generation stays disabled until a real data
    # source is wired up; see app/services/fund_scanner.py and
    # app/services/broker_flow_service.py)
    news_context = await get_news_context([body.symbol])
    fundamentals_context = await get_fundamentals_context([body.symbol])

    # == 2. Build payload for the AI provider ==============================
    payload = build_payload(
        body,
        news_context,
        fundamentals_context=fundamentals_context,
        active_config=runtime_engine.config,
    )

    # == 3. Ask provider ===================================================
    ai_context = build_ai_decision_context(body, news_context=news_context)
    raw = await _provider.decide(ai_context)

    # == 4. Wire into RiskDecision + apply risk engine =====================
    decision = dict_to_risk_decision(raw, body)
    body = await with_resolved_daily_trade_count(body)
    response = runtime_engine.evaluate(body, decision)
    await persist_sizing_audit(body, runtime_engine)

    # == 5. Persist to JSON-lines log ======================================
    await log_signal_evaluation(
        request_id=body.request_id,
        symbol=body.symbol,
        request=body.model_dump(by_alias=True, mode="json"),
        response=response.model_dump(by_alias=True, mode="json"),
    )

    # == 6. Persist to the database ========================================
    await persist_evaluation(body, payload, raw, response)

    return response
