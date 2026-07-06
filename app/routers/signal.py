"""Signal evaluation endpoint — protected by Bearer token.

Flow::

    SignalRequest  →  AiProvider.decide()  →  RiskEngine  →  SignalResponse
                       ↓                         ↓                ↓
                   AiDecision               RiskDecision    market_snapshots
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.core.auth import verify_token
from app.core.logger import log_signal_evaluation
from app.core.risk_config import risk_config
from app.db.session import async_session_factory
from app.models.db import AiDecision as AiDecisionModel
from app.models.db import MarketSnapshot
from app.models.db import RiskDecision as RiskDecisionModel
from app.models.signal import SignalAction, SignalRequest, SignalResponse
from app.services.ai_provider import get_default_provider
from app.services.news_service import get_news_context
from app.services.risk_engine import RiskDecision, RiskEngine

router = APIRouter(tags=["Signal"], dependencies=[Depends(verify_token)])

# ── Engine singletons ─────────────────────────────────────────────────────────

_risk_engine = RiskEngine(risk_config)
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
    # ── 1. Fetch news context ─────────────────────────────────────────────
    news_context = await get_news_context([body.symbol])

    # ── 2. Build payload for the AI provider ──────────────────────────────
    payload = _build_payload(body, news_context)

    # ── 3. Ask provider ───────────────────────────────────────────────────
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

    # ── 6. Persist to PostgreSQL ──────────────────────────────────────────
    await _persist_to_db(body, payload, raw, response)

    return response


# ── Helpers ───────────────────────────────────────────────────────────────────


def _build_payload(req: SignalRequest, news_context: dict[str, Any] | None = None) -> dict:
    """Convert a SignalRequest into a plain dict for the AI provider."""
    payload = {
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
        "lockedLongTermQty": req.locked_long_term_qty,
        "allowedSymbols": sorted(risk_config._allowed_set()),
        "lockedSymbols": sorted(risk_config._locked_set()),
    }
    if news_context:
        payload["newsContext"] = news_context
    return payload


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


# ── Persistence ───────────────────────────────────────────────────────────────


async def _persist_to_db(
    req: SignalRequest,
    payload: dict,
    raw_ai: dict,
    response: SignalResponse,
) -> None:
    """Save evaluation details to PostgreSQL tables.

    Creates one row each in market_snapshots, ai_decisions, and risk_decisions.
    Errors are swallowed so that a DB outage never blocks the signal endpoint.
    """
    try:
        async with async_session_factory() as session:
            # --- market_snapshots ---
            snapshot = MarketSnapshot(
                request_id=req.request_id,
                symbol=req.symbol,
                timeframe=req.timeframe,
                open=req.open,
                high=req.high,
                low=req.low,
                close=req.last_price,
                volume=req.volume,
                rsi=req.rsi,
                ema20=req.ema20,
                ema50=req.ema50,
                macd=req.macd,
                macd_signal=req.macd_signal,
                position_qty=req.bot_position_qty,
                total_account_qty=req.total_account_qty,
                locked_long_term_qty=req.locked_long_term_qty,
                mode=req.mode.value,
            )
            session.add(snapshot)

            # --- ai_decisions ---
            ai_decision = AiDecisionModel(
                request_id=req.request_id,
                symbol=req.symbol,
                provider="deepseek",
                model=None,
                raw_request=payload,
                raw_response=raw_ai,
                action=raw_ai.get("action", "WAIT"),
                confidence=float(raw_ai.get("confidence", 0)),
                qty=float(raw_ai.get("qty", 0)),
                reason=raw_ai.get("reason"),
            )
            session.add(ai_decision)

            # --- risk_decisions ---
            risk_decision = RiskDecisionModel(
                request_id=req.request_id,
                symbol=req.symbol,
                action=response.action.value,
                confidence=response.confidence_score,
                risk_score=response.risk_score,
                allow_order=response.allow_order,
                reason=response.reason,
                entry_min=response.entry_range.min if response.entry_range else None,
                entry_max=response.entry_range.max if response.entry_range else None,
                stop_loss=response.stop_loss,
                target_price=response.target_price,
                order_type=response.order_type.value,
                qty=response.qty,
                mode=req.mode.value,
            )
            session.add(risk_decision)

            await session.commit()

    except Exception:
        # DB is optional for signal flow — never fail the request
        pass
