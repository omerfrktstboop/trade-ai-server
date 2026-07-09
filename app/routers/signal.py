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
    AgenticAction,
    AgenticDataType,
    AgenticSignalRequest,
    AgenticSignalResponse,
    ContextStep,
    EntryRange,
    OrderType,
    SignalAction,
    SignalMode,
    SignalRequest,
    SignalResponse,
)
from app.services.agent_planner import plan_next
from app.services.admin_config import (
    build_runtime_risk_config,
    get_trading_mode_override,
    is_kill_switch_enabled,
)
from app.services.bot_runtime_config import (
    BotConfigMetadata,
    get_bot_config_metadata,
    get_static_bot_config_metadata,
)
from app.services.session_store import (
    SessionState,
    session_store,  # v2 session store singleton
)
from app.services.ai_provider import get_default_provider
from app.services.daily_trade_count import get_today_trade_counts
from app.services.news_service import get_news_context
from app.services.risk_engine import RiskDecision, RiskEngine
from app.services.signal_override import consume_override, override_to_raw_decision

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

    # ── 1. Fetch external context (news only — fund/broker flow context
    # generation is disabled until a real data source is wired up; see
    # app/services/fund_scanner.py and app/services/broker_flow_service.py)
    news_context = await get_news_context([body.symbol])

    # ── 2. Build payload for the AI provider ──────────────────────────────
    payload = _build_payload(
        body,
        news_context,
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
    """Convert a SignalRequest into a plain dict for the AI provider.

    fund_context/broker_flow_context are accepted but currently never
    passed by either live caller (evaluate_signal / evaluate_signal_agent)
    — app/services/fund_scanner.py and app/services/broker_flow_service.py
    still only return empty/UNKNOWN placeholders, and feeding that to the
    AI would just be structured noise. Kept here, disconnected rather than
    removed, so wiring in a real data source later is a one-line change at
    the two call sites instead of a signature change.
    """
    config = active_config or risk_config
    payload = {
        "symbol": req.symbol,
        "timeframe": req.timeframe,
        "lastPrice": req.last_price,
        "open": req.open,
        "high": req.high,
        "low": req.low,
        "volume": req.volume,
        "ohlcReliable": req.ohlc_reliable,
        "rsi": req.rsi,
        "ema20": req.ema20,
        "ema50": req.ema50,
        "macd": req.macd,
        "macdSignal": req.macd_signal,
        "botPositionQty": req.bot_position_qty,
        "totalAccountQty": req.total_account_qty,
        "lockedLongTermQty": req.locked_long_term_qty,
        "dailyTradeCount": req.daily_trade_count,
        "allowedSymbols": sorted(config._allowed_set()),
        "lockedSymbols": sorted(config._locked_set()),
    }
    technical_features = _build_technical_feature_payload(req)
    if technical_features:
        payload.update(technical_features)
        payload["technicalFeatures"] = technical_features
    if news_context:
        payload["newsContext"] = news_context
    if fund_context:
        payload["fundContext"] = fund_context
    if broker_flow_context:
        payload["brokerFlowContext"] = broker_flow_context
    return payload


def _build_technical_feature_payload(req: SignalRequest) -> dict[str, Any]:
    """Return optional Matriks-derived technical features for AI payloads."""
    fields = {
        "alphaTrendSignal": req.alpha_trend_signal,
        "alphaTrendMode": req.alpha_trend_mode,
        "indicatorBuyCount": req.indicator_buy_count,
        "indicatorSellCount": req.indicator_sell_count,
        "indicatorNeutralCount": req.indicator_neutral_count,
        "indicatorConsensus": req.indicator_consensus,
        "indicatorConsensusRatio": req.indicator_consensus_ratio,
        "atr": req.atr,
        "natr": req.natr,
        "adx": req.adx,
        "obvSlope": req.obv_slope,
        "vwapDistancePct": req.vwap_distance_pct,
        "depthBid1Size": req.depth_bid1_size,
        "depthBid1MaxSize": req.depth_bid1_max_size,
        "depthQueueDropPct": req.depth_queue_drop_pct,
        "marketRegime": req.market_regime,
    }
    result = {key: value for key, value in fields.items() if value is not None}
    if result:
        result["schemaVersion"] = "technical-features-v1"
    return result


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


# ── Agent endpoint (v2 — AgenticSignalRequest → AgenticSignalResponse) ─────


def _payload_get(payload: dict[str, Any], key: str, default: Any = None) -> Any:
    if key in payload:
        return payload.get(key)
    nested = payload.get("technicalFeatures")
    if isinstance(nested, dict):
        return nested.get(key, default)
    return default


def _resolve_root_payload(
    agentic: AgenticSignalRequest, session: SessionState | None
) -> dict[str, Any]:
    """Return the payload to build the decision ``SignalRequest`` from.

    ``agentic.market_data`` holds whatever the CURRENT turn's data is — on
    the turn that finally triggers PROCEED, that can be an auxiliary/related
    symbol's data (e.g. THYAO DEPTH fetched for PGSUS's related-symbol check
    — see ``RELATED_SYMBOLS`` in agent_planner.py) rather than the root
    symbol's own data. When a session is available, prefer the root symbol's
    own most-recently-collected step instead, so the decision (RiskEngine
    gates, DB persistence) is never built from a different symbol's price/
    indicator/position data than the one actually being traded.
    """
    if session is not None:
        root = agentic.symbol.strip().upper()
        for step in reversed(session.steps):
            if step.symbol.strip().upper() == root:
                return step.payload
    return agentic.market_data.payload


def _agentic_to_signal_request(
    agentic: AgenticSignalRequest,
    session_id: str = "",
    session: SessionState | None = None,
) -> SignalRequest:
    """Build a :class:`SignalRequest` from the agentic marketData payload.

    Falls back gracefully when fields are missing — the downstream AI and
    RiskEngine handle partial data defensively.
    """
    p = _resolve_root_payload(agentic, session)
    return SignalRequest(
        requestId=agentic.request_id,
        symbol=agentic.symbol,
        timeframe=p.get("timeframe", "1h"),
        lastPrice=p.get("lastPrice", p.get("close", 0)),
        open=p.get("open", 0),
        high=p.get("high", 0),
        low=p.get("low", 0),
        volume=p.get("volume", 0),
        ohlcReliable=p.get("ohlcReliable"),
        rsi=p.get("rsi") or p.get("rsi14"),
        ema20=p.get("ema20"),
        ema50=p.get("ema50"),
        macd=p.get("macd"),
        macdSignal=p.get("macdSignal"),
        alphaTrendSignal=_payload_get(p, "alphaTrendSignal"),
        alphaTrendMode=_payload_get(p, "alphaTrendMode"),
        indicatorBuyCount=_payload_get(p, "indicatorBuyCount"),
        indicatorSellCount=_payload_get(p, "indicatorSellCount"),
        indicatorNeutralCount=_payload_get(p, "indicatorNeutralCount"),
        indicatorConsensus=_payload_get(p, "indicatorConsensus"),
        indicatorConsensusRatio=_payload_get(p, "indicatorConsensusRatio"),
        atr=_payload_get(p, "atr"),
        natr=_payload_get(p, "natr"),
        adx=_payload_get(p, "adx"),
        obvSlope=_payload_get(p, "obvSlope"),
        vwapDistancePct=_payload_get(p, "vwapDistancePct"),
        depthBid1Size=_payload_get(p, "depthBid1Size"),
        depthBid1MaxSize=_payload_get(p, "depthBid1MaxSize"),
        depthQueueDropPct=_payload_get(p, "depthQueueDropPct"),
        marketRegime=_payload_get(p, "marketRegime"),
        botPositionQty=p.get("botPositionQty", 0),
        totalAccountQty=p.get("totalAccountQty", 0),
        lockedLongTermQty=p.get("lockedLongTermQty", 0),
        dailyTradeCount=p.get("dailyTradeCount", 0),
        sessionId=session_id,
        mode=agentic.mode,
    )


def _agentic_waiter(
    request_id: str,
    session_id: str,
    reason: str,
    symbol: str = "",
    *,
    proceed_to_ai: bool = False,
    config_metadata: BotConfigMetadata | None = None,
) -> AgenticSignalResponse:
    """Shortcut: return a WAIT response with allowOrder=False."""
    metadata = config_metadata or get_static_bot_config_metadata()
    return AgenticSignalResponse(
        requestId=request_id,
        sessionId=session_id,
        symbol=symbol,
        action=AgenticAction.WAIT,
        allowOrder=False,
        requiresConfirmation=False,
        reason=reason,
        confidenceScore=0.0,
        riskScore=0.0,
        qty=0.0,
        orderType=OrderType.NONE,
        configVersion=metadata.config_version,
        configHash=metadata.config_hash,
    )


async def _load_bot_config_metadata() -> BotConfigMetadata:
    try:
        async with async_session_factory() as session:
            return await get_bot_config_metadata(session)
    except Exception:
        logger.exception("Failed to load bot config metadata")
        return get_static_bot_config_metadata()


@router.post("/signal/evaluate-agent")
async def evaluate_signal_agent(body: AgenticSignalRequest) -> AgenticSignalResponse:
    """Evaluate a trading signal with agentic multi-turn data gathering (v2).

    Accepts :class:`AgenticSignalRequest` and returns :class:`AgenticSignalResponse`.
    Uses SessionState (Pydantic) + SessionStore (in-memory RLock).

    Flow:
    1. Parse AgenticSignalRequest — extract marketData + contextHistory
    2. Create or retrieve session
    3. Append marketData as ContextStep (with actual dataType from request)
    4. Append contextHistory steps if provided
    5. Planner → FETCH_DATA or PROCEED
    6. FETCH_DATA → validate targetSymbol, toolCallCount → return with top-level fields
    7. PROCEED → build SignalRequest bridge → AI.decide() → RiskEngine → final
    """
    # ── Kill switch check (async, needs DB) ────────────────────────────
    req_id = body.request_id
    symbol = body.symbol
    raw_session_id = body.session_id
    bot_config_metadata = await _load_bot_config_metadata()

    try:
        async with async_session_factory() as session:
            kill_switch_enabled = await is_kill_switch_enabled(session)
    except Exception:
        logger.exception("Failed to check kill switch for agent endpoint")
        kill_switch_enabled = False

    if kill_switch_enabled:
        return _agentic_waiter(
            req_id, raw_session_id or "",
            "Kill switch enabled: trading disabled by admin",
            symbol,
            config_metadata=bot_config_metadata,
        )

    # ── 1-2: Session management ────────────────────────────────────────
    session_store.cleanup_expired_sessions()

    if raw_session_id:
        sess = session_store.get_session(raw_session_id)
        if sess is None:
            return _agentic_waiter(
                req_id, raw_session_id,
                "Session expired or not found",
                symbol,
                config_metadata=bot_config_metadata,
            )
    else:
        sess = session_store.create_session(symbol)

    # ── 3: Append marketData as ContextStep ────────────────────────────
    md = body.market_data
    step_no = len(sess.steps) + 1
    step = ContextStep(
        stepNo=step_no,
        symbol=md.symbol,
        dataType=md.data_type,  # <-- actual dataType from request, not hardcoded
        payload=md.payload,
        reason=f"Market data: {md.data_type.value}",
    )
    session_store.append_step(sess.session_id, step)

    # ── 4: Append contextHistory steps ─────────────────────────────────
    # The Matriks bot rebuilds its own rolling history on every FETCH_DATA
    # response (TradeAiAgenticBot.cs::FetchRequestedDataAsync appends the
    # previous marketData as a "Previous marketData" step) and resends it
    # here — but the server already recorded that same step itself when it
    # first received that marketData (see step 3 above, on the earlier
    # turn). Skip anything already collected so agenticSteps sent to the AI
    # doesn't accumulate duplicate entries turn after turn.
    already_collected = {(s.symbol.strip().upper(), s.data_type) for s in sess.steps}
    for hist_step in body.context_history:
        key = (hist_step.symbol.strip().upper(), hist_step.data_type)
        if key in already_collected:
            continue
        session_store.append_step(sess.session_id, hist_step)
        already_collected.add(key)

    # ── 5: Planner decides next action ──────────────────────────────────
    # Build runtime risk config once, up front, so both the planner's initial
    # symbol-allow check and the FETCH_DATA check below see the same
    # admin-panel-edited allow-list (no stale static-singleton gate).
    try:
        async with async_session_factory() as db_sess:
            runtime_cfg = await build_runtime_risk_config(db_sess)
    except Exception:
        runtime_cfg = risk_config

    plan = plan_next(sess, runtime_cfg)

    # ── 6: FETCH_DATA path ──────────────────────────────────────────────
    if plan.action == AgenticAction.FETCH_DATA and plan.target_symbol:
        target = plan.target_symbol

        if not runtime_cfg.is_symbol_allowed(target):
            return _agentic_waiter(
                req_id, sess.session_id,
                f"Target symbol {target} is not in the allowed list",
                symbol,
                config_metadata=bot_config_metadata,
            )

        if not sess.can_tool_call:
            return _agentic_waiter(
                req_id, sess.session_id,
                "Tool call limit reached — cannot FETCH_DATA",
                symbol,
                config_metadata=bot_config_metadata,
            )

        session_store.increment_tool_call(sess.session_id)
        return AgenticSignalResponse(
            requestId=req_id,
            sessionId=sess.session_id,
            symbol=symbol,
            action=AgenticAction.FETCH_DATA,
            allowOrder=False,
            requiresConfirmation=False,
            reason=plan.reason,
            targetSymbol=target,
            requiredDataType=plan.required_data_type,
            confidenceScore=0.0,
            riskScore=0.0,
            qty=0.0,
            orderType=OrderType.NONE,
            configVersion=bot_config_metadata.config_version,
            configHash=bot_config_metadata.config_hash,
        )

    # ── 7: WAIT from planner (hard stop — e.g. symbol not allowed) ────
    if plan.action == AgenticAction.WAIT and not plan.proceed_to_ai:
        session_store.close_session(sess.session_id)
        return _agentic_waiter(
            req_id,
            sess.session_id,
            plan.reason,
            symbol,
            config_metadata=bot_config_metadata,
        )

    # ── 8: PROCEED — build bridge to AI + RiskEngine ────────────────────
    sig_req = _agentic_to_signal_request(body, sess.session_id, session=sess)

    # Runtime controls on the built SignalRequest
    sig_req, runtime_engine, _ks = await _with_runtime_controls(sig_req)

    # Fetch external context (news only — fund/broker flow context
    # generation is disabled until a real data source is wired up; see
    # app/services/fund_scanner.py and app/services/broker_flow_service.py)
    news_context = await get_news_context([sig_req.symbol])

    payload = _build_payload(
        sig_req,
        news_context=news_context,
        active_config=runtime_engine.config,
    )
    # Inject accumulated session steps into AI payload
    payload["agenticSteps"] = [s.model_dump(by_alias=True) for s in sess.steps]

    # Manual test override — bypass the AI for this symbol's next evaluation
    # only (never in REAL_LIVE, so a test-only feature can't move real capital).
    raw = None
    if sig_req.mode in (SignalMode.PAPER, SignalMode.MANUAL, SignalMode.DEMO_LIVE):
        try:
            async with async_session_factory() as ov_session:
                override = await consume_override(ov_session, sig_req.symbol)
            if override is not None:
                raw = override_to_raw_decision(override)
        except Exception:
            logger.exception(
                "Failed to check signal override for %s", sig_req.symbol
            )

    if raw is None:
        raw = await _provider.decide(payload)
    decision = _dict_to_risk_decision(raw, sig_req)
    sig_req = await _with_resolved_daily_trade_count(sig_req)

    response = runtime_engine.evaluate(sig_req, decision)

    # Close session (final decision)
    session_store.close_session(sess.session_id)

    # Log + persist
    log_signal_evaluation(
        request_id=req_id,
        symbol=sig_req.symbol,
        mode=sig_req.mode.value,
        request=sig_req.model_dump(by_alias=True, exclude={"mode"}),
        response=response.model_dump(by_alias=True),
    )
    await _persist_to_db(sig_req, payload, raw, response)

    return AgenticSignalResponse(
        requestId=response.request_id,
        sessionId=sess.session_id,
        symbol=sig_req.symbol,
        action=AgenticAction(response.action.value),
        allowOrder=response.allow_order,
        requiresConfirmation=response.requires_confirmation,
        reason=response.reason,
        confidenceScore=response.confidence_score,
        riskScore=response.risk_score,
        qty=response.qty,
        orderType=response.order_type,
        price=response.price,
        entryRange=response.entry_range,
        stopLoss=response.stop_loss,
        targetPrice=response.target_price,
        configVersion=bot_config_metadata.config_version,
        configHash=bot_config_metadata.config_hash,
    )
