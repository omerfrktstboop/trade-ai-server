"""Persistence for evaluator decisions: RiskDecision construction from raw
AI output, and writing AiDecision/RiskDecision/PositionSizingAudit rows.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import AIProvider, settings
from app.db.session import async_session_factory

from app.models.db import AiDecision as AiDecisionModel
from app.models.db import MarketSnapshot
from app.models.db import PositionSizingAudit
from app.models.db import RiskDecision as RiskDecisionModel
from app.models.signal import (
    EntryRange,
    SignalAction,
    SignalRequest,
    SignalResponse,
)
from app.services.evaluation.parsing import _safe_action, _safe_decimal, _safe_float
from app.services.risk_engine import RiskDecision, RiskEngine

logger = logging.getLogger(__name__)


def _json_safe(value: Any) -> Any:
    """Return a JSON-compatible copy for DB JSON columns."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _decision_persistence_metadata(payload: dict[str, Any]) -> tuple[str, str | None]:
    """Label AI-decision rows accurately without claiming a model was called."""
    source = str(payload.get("decisionSource") or "system-gate")
    if source != "llm":
        return source, None
    model = (
        settings.deepseek_model if settings.ai_provider == AIProvider.DEEPSEEK else None
    )
    return settings.ai_provider.value, model


def dict_to_risk_decision(raw: dict, _req: SignalRequest | None = None) -> RiskDecision:
    """Parse a provider response dict into a RiskDecision.

    Every field is parsed defensively - no matter what garbage the AI
    returns, this function will not raise. Invalid actions fall back to
    WAIT, non-numeric fields default to 0.
    """
    action = _safe_action(raw.get("action"))
    fallbacks: list[str] = []

    if action == SignalAction.WAIT and raw.get("action") not in (
        None,
        "WAIT",
        "BUY",
        "SELL",
    ):
        fallbacks.append(f"Invalid AI action '{raw.get('action')}', fallback WAIT")

    reason = str(raw.get("reason") or "Provider returned no reason")
    if fallbacks:
        reason = reason + " | " + " | ".join(fallbacks)

    return RiskDecision(
        action=action,
        confidence=_safe_float(raw.get("confidence")),
        risk_score=_safe_float(raw.get("risk_score")),
        reason=reason,
        qty=0,
        entry_range=_parse_entry_range(raw),
        stop_loss=_safe_decimal(raw.get("stop_loss") or raw.get("stopLoss")),
        target_price=_safe_decimal(raw.get("target_price") or raw.get("targetPrice")),
    )


def _parse_entry_range(raw: dict) -> EntryRange | None:
    """Parse entryRange from AI response (supports camelCase + snake_case).

    Never raises - garbage values produce None.
    """
    # camelCase nested: {"entryRange": {"min": 100, "max": 105}}
    entry_range = raw.get("entryRange") or raw.get("entry_range")
    if isinstance(entry_range, dict):
        mn = (
            entry_range.get("min")
            or entry_range.get("entryMin")
            or entry_range.get("entry_min")
        )
        mx = (
            entry_range.get("max")
            or entry_range.get("entryMax")
            or entry_range.get("entry_max")
        )
        if mn is not None and mx is not None:
            mn = _safe_decimal(mn)
            mx = _safe_decimal(mx)
            if mn is not None and mx is not None:
                return EntryRange(min=mn, max=mx)

    # Flat camelCase: {"entryMin": 100, "entryMax": 105}
    entry_min = raw.get("entryMin") or raw.get("entry_min")
    entry_max = raw.get("entryMax") or raw.get("entry_max")
    if entry_min is not None and entry_max is not None:
        entry_min = _safe_decimal(entry_min)
        entry_max = _safe_decimal(entry_max)
        if entry_min is not None and entry_max is not None:
            return EntryRange(min=entry_min, max=entry_max)

    return None


async def persist_evaluation(
    req: SignalRequest,
    payload: dict,
    raw_ai: dict,
    response: SignalResponse,
) -> None:
    """Save evaluation details to the database.

    Creates one row each in market_snapshots, ai_decisions, and risk_decisions.
    Errors are swallowed so that a DB outage never blocks evaluation.
    """
    try:
        provider_name, model_name = _decision_persistence_metadata(payload)
        async with async_session_factory() as session:
            session.add(
                MarketSnapshot(
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
                    spread_pct=req.spread_pct,
                    bid_ask_ratio_top5=req.depth_bid_ask_ratio_top5,
                    bid_ask_ratio_top10=req.depth_bid_ask_ratio_top10,
                    bid_ask_ratio_top25=req.depth_bid_ask_ratio_top25,
                    imbalance_top10=req.depth_imbalance_top10,
                    imbalance_top25=req.depth_imbalance_top25,
                    largest_bid_wall_distance_pct=req.depth_largest_bid_wall_distance_pct,
                    largest_ask_wall_distance_pct=req.depth_largest_ask_wall_distance_pct,
                    depth_buy_pressure_score=req.depth_buy_pressure_score,
                    depth_sell_pressure_score=req.depth_sell_pressure_score,
                    depth_order_book_signal=req.depth_order_book_signal,
                    depth_reliable=req.depth_reliable,
                    position_qty=req.bot_position_qty,
                    total_account_qty=req.total_account_qty,
                    locked_long_term_qty=req.locked_long_term_qty,
                )
            )
            # Cache tekrarinda saglayicinin eski gecikmesini yazmak yaniltici
            # olur - sure yalnizca gercek LLM cagrisinda kaydedilir.
            response_time_ms = (
                raw_ai.get("_response_time_ms")
                if payload.get("decisionSource") == "llm"
                else None
            )
            session.add(
                AiDecisionModel(
                    request_id=req.request_id,
                    symbol=req.symbol,
                    provider=provider_name,
                    model=model_name,
                    raw_request=_json_safe(payload),
                    raw_response=_json_safe(raw_ai.get("_audit_raw_response", raw_ai)),
                    action=raw_ai.get("action", "WAIT"),
                    confidence=float(raw_ai.get("confidence", 0)),
                    qty=0,
                    reason=raw_ai.get("reason"),
                    response_time_ms=response_time_ms,
                )
            )
            session.add(
                RiskDecisionModel(
                    request_id=req.request_id,
                    symbol=req.symbol,
                    action=response.action.value,
                    confidence=response.confidence_score,
                    risk_score=response.risk_score,
                    allow_order=response.allow_order,
                    reason=response.reason,
                    entry_min=response.entry_range.min
                    if response.entry_range
                    else None,
                    entry_max=response.entry_range.max
                    if response.entry_range
                    else None,
                    stop_loss=response.stop_loss,
                    target_price=response.target_price,
                    order_type=response.order_type.value,
                    qty=response.qty,
                )
            )
            from app.services.outcome_tracking import create_decision_outcome

            await create_decision_outcome(session, req, payload, raw_ai, response)
            await session.commit()

    except Exception:
        # DB is optional for the evaluation flow - never fail the caller
        logger.exception(
            "Failed to persist signal evaluation request_id=%s symbol=%s",
            req.request_id,
            req.symbol,
        )


async def persist_sizing_audit(req: SignalRequest, engine: RiskEngine) -> None:
    """Persist the exact server-side sizing inputs and result, without secrets."""
    result = engine.last_sizing_result
    trade = engine.last_sizing_trade
    limits = engine.effective_config
    account = req.account_sizing_context
    if result is None or limits is None or account is None or trade is None:
        return
    details = result.calculation_details
    try:
        async with async_session_factory() as session:
            session.add(
                PositionSizingAudit(
                    request_id=req.request_id,
                    symbol=req.symbol,
                    trade_profile_id=limits.trade_profile_id,
                    trade_profile_version=limits.trade_profile_version,
                    system_config_version=limits.system_config_version,
                    environment_config_fingerprint=(
                        limits.environment_config_fingerprint
                    ),
                    account_equity_tl=account.account_equity_tl,
                    effective_available_cash_tl=(account.effective_available_cash_tl),
                    risk_per_trade_pct=limits.risk_per_trade_pct,
                    risk_budget_tl=result.risk_budget_tl,
                    entry_price=trade.entry_price,
                    stop_loss=trade.stop_loss,
                    raw_stop_distance_tl=result.raw_stop_distance_tl,
                    slippage_buffer_tl=result.slippage_buffer_tl,
                    effective_stop_distance_tl=result.effective_stop_distance_tl,
                    qty_by_risk=details.get("qty_by_risk"),
                    qty_by_cash=details.get("qty_by_cash"),
                    qty_by_account_exposure=details.get("qty_by_account_exposure"),
                    qty_by_symbol_position=details.get("qty_by_symbol_position"),
                    qty_by_order_value=details.get("qty_by_order_value"),
                    qty_by_profile_max=details.get("qty_by_profile_max"),
                    final_qty=result.qty,
                    order_value_tl=result.order_value_tl,
                    estimated_loss_at_stop_tl=result.estimated_loss_at_stop_tl,
                    binding_limits=result.binding_limits,
                    allowed=result.allowed,
                    reason=result.reason,
                    effective_risk_config=limits.model_dump(mode="json"),
                    calculation_details=result.model_dump(mode="json")[
                        "calculation_details"
                    ],
                )
            )
            await session.commit()
    except Exception:
        logger.exception(
            "Failed to persist sizing audit request_id=%s symbol=%s",
            req.request_id,
            req.symbol,
        )
