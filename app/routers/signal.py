"""Signal evaluation endpoint — protected by Bearer token.

Flow::

    SignalRequest  →  AiProvider.decide()  →  RiskEngine  →  SignalResponse
                       ↓                         ↓                ↓
                   AiDecision               RiskDecision    market_snapshots
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends

from app.core.auth import verify_token
from app.core.logger import log_signal_evaluation
from app.core.risk_config import RiskConfig, risk_config
from app.db.session import async_session_factory
from app.models.db import AiDecision as AiDecisionModel
from app.models.db import MarketSnapshot
from app.models.db import RiskDecision as RiskDecisionModel
from app.models.signal import (
    AgentAction,
    AgenticDataType,
    AgentSignalResponse,
    ContextStep,
    EntryRange,
    FetchData,
    OrderType,
    SignalAction,
    SignalRequest,
    SignalResponse,
)
from app.services.agent_planner import plan_next
from app.services.admin_config import (
    build_runtime_risk_config,
    get_trading_mode_override,
    is_kill_switch_enabled,
)
from app.services.session_store import (
    session_store,  # v2 session store singleton
)
from app.services.ai_provider import get_default_provider
from app.services.broker_flow_service import get_broker_flow_context
from app.services.daily_trade_count import get_today_trade_counts
from app.services.fund_scanner import get_fund_context
from app.services.news_service import get_news_context
from app.services.risk_engine import RiskDecision, RiskEngine

logger = logging.getLogger(__name__)

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
    body, runtime_engine, kill_switch_enabled = await _with_runtime_controls(body)
    if kill_switch_enabled:
        response = _kill_switch_response(body)
        raw = {
            "action": "WAIT",
            "confidence": 0.0,
            "risk_score": 0.0,
            "reason": response.reason,
        }
        payload = _build_payload(body, active_config=runtime_engine.config)
        log_signal_evaluation(
            request_id=body.request_id,
            symbol=body.symbol,
            mode=body.mode.value,
            request=body.model_dump(by_alias=True, exclude={"mode"}),
            response=response.model_dump(by_alias=True),
        )
        await _persist_to_db(body, payload, raw, response)
        return response

    # ── 1. Fetch external context (news + fund + broker flows) ────────────
    news_context = await get_news_context([body.symbol])
    fund_context = await get_fund_context([body.symbol])
    broker_flow_context = await get_broker_flow_context([body.symbol])

    # ── 2. Build payload for the AI provider ──────────────────────────────
    payload = _build_payload(
        body,
        news_context,
        fund_context,
        broker_flow_context,
        active_config=runtime_engine.config,
    )

    # ── 3. Ask provider ───────────────────────────────────────────────────
    raw = await _provider.decide(payload)

    # ── 3. Wire into RiskDecision ─────────────────────────────────────────
    decision = _dict_to_risk_decision(raw, body)
    body = await _with_resolved_daily_trade_count(body)

    # ── 4. Apply risk engine ──────────────────────────────────────────────
    response = runtime_engine.evaluate(body, decision)

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


def _build_payload(
    req: SignalRequest,
    news_context: dict[str, Any] | None = None,
    fund_context: dict[str, Any] | None = None,
    broker_flow_context: dict[str, Any] | None = None,
    active_config: RiskConfig | None = None,
) -> dict:
    """Convert a SignalRequest into a plain dict for the AI provider."""
    config = active_config or risk_config
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
        "allowedSymbols": sorted(config._allowed_set()),
        "lockedSymbols": sorted(config._locked_set()),
    }
    if news_context:
        payload["newsContext"] = news_context
    if fund_context:
        payload["fundContext"] = fund_context
    if broker_flow_context:
        payload["brokerFlowContext"] = broker_flow_context
    return payload


async def _with_runtime_controls(
    req: SignalRequest,
) -> tuple[SignalRequest, RiskEngine, bool]:
    """Apply DB-backed runtime config controls when available."""
    try:
        async with async_session_factory() as session:
            runtime_config = await build_runtime_risk_config(session)
            mode_override = await get_trading_mode_override(session)
            kill_switch_enabled = await is_kill_switch_enabled(session)
    except Exception:
        logger.exception(
            "Failed to load runtime admin config request_id=%s symbol=%s",
            req.request_id,
            req.symbol,
        )
        return req, _risk_engine, False

    if mode_override is not None:
        req = req.model_copy(update={"mode": mode_override})
    return req, RiskEngine(runtime_config), kill_switch_enabled


def _kill_switch_response(req: SignalRequest) -> SignalResponse:
    return SignalResponse(
        requestId=req.request_id,
        symbol=req.symbol,
        action=SignalAction.WAIT,
        qty=0.0,
        orderType=OrderType.NONE,
        price=None,
        confidenceScore=0.0,
        riskScore=0.0,
        allowOrder=False,
        requiresConfirmation=False,
        reason="Kill switch enabled: trading disabled by admin",
        entryRange=None,
        stopLoss=None,
        targetPrice=None,
    )


async def _with_resolved_daily_trade_count(req: SignalRequest) -> SignalRequest:
    """Fill dailyTradeCount from DB only when the caller omitted it."""
    if _has_explicit_daily_trade_count(req):
        return req

    try:
        async with async_session_factory() as session:
            counts = await get_today_trade_counts(session, req.symbol)
    except Exception:
        logger.exception(
            "Failed to resolve daily trade count from DB request_id=%s symbol=%s",
            req.request_id,
            req.symbol,
        )
        return req

    logger.info(
        "Resolved daily trade count from DB symbol=%s symbol_count=%s bot_count=%s effective=%s",
        counts.symbol,
        counts.symbol_count,
        counts.bot_count,
        counts.effective_count,
    )
    return req.model_copy(update={"daily_trade_count": counts.effective_count})


def _has_explicit_daily_trade_count(req: SignalRequest) -> bool:
    """Return True when dailyTradeCount was present in the request payload."""
    return bool({"daily_trade_count", "dailyTradeCount"} & req.model_fields_set)


def _safe_action(raw_value: Any) -> SignalAction:
    """Parse action string safely — invalid values fall back to WAIT."""
    if not raw_value:
        return SignalAction.WAIT
    try:
        action = SignalAction(str(raw_value).upper())
        return action
    except ValueError:
        return SignalAction.WAIT


def _safe_float(raw_value: Any, default: Any = 0.0) -> Any:
    """Parse a float safely — non-numeric values return the default."""
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except (ValueError, TypeError):
        return default


def _dict_to_risk_decision(raw: dict, _req: SignalRequest) -> RiskDecision:
    """Parse a provider response dict into a RiskDecision.

    Every field is parsed defensively — no matter what garbage the AI
    returns, this function will not raise.  Invalid actions fall back
    to WAIT, non-numeric fields default to 0.
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
        qty=_safe_float(raw.get("qty")),
        entry_range=_parse_entry_range(raw),
        stop_loss=_safe_float(raw.get("stop_loss") or raw.get("stopLoss"), default=0.0)
        or None,
        target_price=_safe_float(
            raw.get("target_price") or raw.get("targetPrice"), default=0.0
        )
        or None,
    )


def _parse_entry_range(raw: dict) -> EntryRange | None:
    """Parse entryRange from AI response (supports camelCase + snake_case).

    Never raises — garbage values produce None.
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
            mn = _safe_float(mn, default=None)
            mx = _safe_float(mx, default=None)
            if mn is not None and mx is not None:
                return EntryRange(min=mn, max=mx)

    # Flat camelCase: {"entryMin": 100, "entryMax": 105}
    entry_min = raw.get("entryMin") or raw.get("entry_min")
    entry_max = raw.get("entryMax") or raw.get("entry_max")
    if entry_min is not None and entry_max is not None:
        entry_min = _safe_float(entry_min, default=None)
        entry_max = _safe_float(entry_max, default=None)
        if entry_min is not None and entry_max is not None:
            return EntryRange(min=entry_min, max=entry_max)

    return None


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
        logger.exception(
            "Failed to persist signal evaluation request_id=%s symbol=%s",
            req.request_id,
            req.symbol,
        )


# ── Agent endpoint (v2 — SessionState + session_store) ──────────────────────


@router.post("/signal/evaluate-agent")
async def evaluate_signal_agent(body: SignalRequest) -> AgentSignalResponse:
    """Evaluate a trading signal with agentic multi-turn data gathering (v2).

    Uses SessionState (Pydantic) + SessionStore (in-memory RLock) for
    session management instead of the v1 AgentSession.

    Flow:
    1. Request al - parse SignalRequest
    2. sessionId yoksa yeni session olustur (session_store.create_session)
    3. sessionId varsa session_store'dan bul - expired/missing -> WAIT
    4. Gelen marketData'yi ContextStep olarak session'a ekle
    5. Planner calistir (rule-based -> FETCH_DATA or PROCEED)
    6. FETCH_DATA -> validate targetSymbol, requiredDataType, toolCallCount
    7. PROCEED -> AI.decide() -> RiskEngine -> close_session -> final response
    """
    body, runtime_engine, kill_switch_enabled = await _with_runtime_controls(body)
    request_id = body.request_id
    raw_session_id = body.session_id
    extra = body.model_extra or {}
    symbol = body.symbol

    if kill_switch_enabled:
        return AgentSignalResponse(
            requestId=request_id,
            symbol=symbol,
            sessionId=raw_session_id or "",
            action=AgentAction.WAIT,
            allowOrder=False,
            reason="Kill switch enabled: trading disabled by admin",
        )

    # ── 1-2-3: Session management ────────────────────────────────────
    session_store.cleanup_expired_sessions()

    if raw_session_id:
        session = session_store.get_session(raw_session_id)
        if session is None:
            return AgentSignalResponse(
                requestId=request_id,
                symbol=symbol,
                sessionId=raw_session_id,
                action=AgentAction.WAIT,
                reason="Session expired or not found",
            )
    else:
        session = session_store.create_session(symbol)

    # ── 4: Append marketData as ContextStep ──────────────────────────
    market_data = body.model_dump(by_alias=True)
    step_no = len(session.steps) + 1
    step = ContextStep(
        stepNo=step_no,
        symbol=symbol,
        dataType=AgenticDataType.OHLCV,
        payload=market_data,
        reason="Market data snapshot",
    )
    session_store.append_step(session.session_id, step)

    # ── 5: Planner decides next action ────────────────────────────────
    plan = plan_next(session)

    # ── 6: FETCH_DATA path (data request) ─────────────────────────────
    if plan.action == AgentAction.FETCH_DATA and plan.fetch_data is not None:
        fd = plan.fetch_data
        # Validate targetSymbol ∈ allowedSymbols
        if not runtime_engine.config.is_symbol_allowed(fd.target_symbol):
            return AgentSignalResponse(
                requestId=request_id,
                symbol=symbol,
                sessionId=session.session_id,
                action=AgentAction.WAIT,
                reason=f"Target symbol {fd.target_symbol} is not in the allowed list",
            )
        # Check toolCallCount < maxToolCallsPerSession
        if not session.can_tool_call:
            return AgentSignalResponse(
                requestId=request_id,
                symbol=symbol,
                sessionId=session.session_id,
                action=AgentAction.WAIT,
                reason="Tool call limit reached — cannot FETCH_DATA",
            )
        session_store.increment_tool_call(session.session_id)
        return AgentSignalResponse(
            requestId=request_id,
            symbol=symbol,
            sessionId=session.session_id,
            action=AgentAction.FETCH_DATA,
            fetchData=fd,
            allowOrder=False,
            reason=plan.reason,
        )

    # ── 7: WAIT from planner (invalid symbol etc.) ────────────────────
    if plan.action == AgentAction.WAIT and plan.fetch_data is None:
        session_store.close_session(session.session_id)
        return AgentSignalResponse(
            requestId=request_id,
            symbol=symbol,
            sessionId=session.session_id,
            action=AgentAction.WAIT,
            allowOrder=False,
            reason=plan.reason,
        )

    # ── 8: PROCEED — AI + RiskEngine ──────────────────────────────────
    news_context = extra.get("newsContext")
    fund_context = extra.get("fundContext")
    broker_flow_context = extra.get("brokerFlowContext")
    payload = _build_payload(
        body,
        news_context=news_context,
        fund_context=fund_context,
        broker_flow_context=broker_flow_context,
        active_config=runtime_engine.config,
    )
    # Inject accumulated session steps into payload as context
    payload["agenticSteps"] = [s.model_dump(by_alias=True) for s in session.steps]

    raw = await _provider.decide(payload)
    decision = _dict_to_risk_decision(raw, body)
    body_enriched = await _with_resolved_daily_trade_count(body)

    response = runtime_engine.evaluate(body_enriched, decision)

    # ── Close session (final decision) ───────────────────────────────
    session_store.close_session(session.session_id)

    # ── Log + persist ─────────────────────────────────────────────────
    log_signal_evaluation(
        request_id=request_id,
        symbol=body_enriched.symbol,
        mode=body_enriched.mode.value,
        request=body_enriched.model_dump(by_alias=True, exclude={"mode"}),
        response=response.model_dump(by_alias=True),
    )
    await _persist_to_db(body_enriched, payload, raw, response)

    return AgentSignalResponse(
        requestId=response.request_id,
        symbol=response.symbol,
        sessionId=session.session_id,
        action=AgentAction(response.action.value),
        qty=response.qty,
        orderType=response.order_type,
        price=response.price,
        confidenceScore=response.confidence_score,
        riskScore=response.risk_score,
        allowOrder=response.allow_order,
        requiresConfirmation=response.requires_confirmation,
        reason=response.reason,
        entryRange=response.entry_range,
        stopLoss=response.stop_loss,
        targetPrice=response.target_price,
    )
