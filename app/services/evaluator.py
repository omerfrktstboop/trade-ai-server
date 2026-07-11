"""In-process signal evaluator — full-inversion mimarisinin beyni.

Eski FETCH_DATA/session protokolünün (``/api/signal/evaluate-agent`` +
``agent_session`` + ``session_store`` + ``agent_planner`` + HTTP ping-pong)
yerine geçer: veri toplama artık ağ üzerinden çok turlu bir oturum değil,
bu modül içinde senkron gateway çağrılarıdır.

Akış::

    gateway.get_snapshot(root)                      # OHLCV+DEPTH+TECHNICAL tek çağrıda
      └─ RELATED_SYMBOLS[root] varsa ek snapshot    # ör. ANELE → THYAO derinliği
    → SignalRequest köprüsü
    → runtime kontroller (kill switch, mode override, runtime risk config)
    → news + fundamentals bağlamı
    → admin test override VEYA AI provider
    → RiskEngine
    → log + DB persist (market_snapshots / ai_decisions / risk_decisions)

Bu modül aynı zamanda değerlendirme boru hattının paylaşılan yardımcılarına
ev sahipliği yapar (``build_payload``, ``dict_to_risk_decision``,
``with_runtime_controls``, ``persist_evaluation`` …). ``/api/signal/evaluate``
router'ı bunları buradan alır — beyin serviste, HTTP katmanı ince.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.logger import log_signal_evaluation
from app.core.risk_config import RiskConfig, risk_config
from app.db.session import async_session_factory
from sqlalchemy import select

from app.models.db import AiDecision as AiDecisionModel
from app.models.db import BotPosition as BotPositionModel
from app.models.db import MarketSnapshot
from app.models.db import RiskDecision as RiskDecisionModel
from app.models.signal import (
    EntryRange,
    OrderType,
    SignalAction,
    SignalMode,
    SignalRequest,
    SignalResponse,
)
from app.services.ai_provider import AiProvider, get_default_provider
from app.services.admin_config import (
    build_runtime_risk_config,
    get_trading_mode_override,
    is_kill_switch_enabled,
)
from app.services.broker_flow_service import get_broker_flow_context
from app.services.daily_trade_count import get_today_trade_counts
from app.services.decision_gate import decision_cache, preflight_wait_reason
from app.services.fundamentals_service import get_fundamentals_context
from app.services.market_regime import get_index_regime
from app.services.matriks_gateway import (
    GatewayError,
    MatriksGatewayClient,
    gateway_client,
)
from app.services.news_service import get_news_context
from app.services.risk_engine import RiskDecision, RiskEngine
from app.services.signal_override import consume_override, override_to_raw_decision

logger = logging.getLogger(__name__)

# Statik singleton — runtime config yüklenemediğinde kullanılan yedek motor.
_static_risk_engine = RiskEngine(risk_config)

# Kök sembol değerlendirilirken derinliği de çekilen ilişkili hisseler.
# (Eski agent_planner.RELATED_SYMBOLS — planner silindi, kural burada yaşıyor.)
RELATED_SYMBOLS: dict[str, str] = {
    "ANELE": "THYAO",
    "PGSUS": "THYAO",
    "TUPRS": "KCHOL",
}


@dataclass(frozen=True)
class EvaluationResult:
    """Final karar + kararın alındığı efektif mod.

    ``mode`` runtime override'lar ve ``force_paper`` uygulandıktan sonraki
    değerdir — scanner'ın emir gönderme kapısı bu alana bakar (SignalResponse
    mode taşımaz).
    """

    response: SignalResponse
    mode: SignalMode


# ═══════════════════════════════════════════════════════════════════════════════
# AI payload oluşturma
# ═══════════════════════════════════════════════════════════════════════════════


def build_payload(
    req: SignalRequest,
    news_context: dict[str, Any] | None = None,
    fund_context: dict[str, Any] | None = None,
    broker_flow_context: dict[str, Any] | None = None,
    fundamentals_context: dict[str, Any] | None = None,
    active_config: RiskConfig | None = None,
) -> dict:
    """Convert a SignalRequest into a plain dict for the AI provider.

    ``news_context``, ``broker_flow_context`` (smart-money / AKD flow) and
    ``fundamentals_context`` are live: the scanner/evaluate flow fetches them
    per symbol and passes them here. ``fund_context`` (fund_scanner) is still
    a placeholder and normally not passed — kept in the signature so wiring a
    real source later is a one-line change at the call site.
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
        "ohlcSource": req.ohlc_source,
        "quoteReliable": req.quote_reliable,
        "priceSource": req.price_source,
        "depthReliable": req.depth_reliable,
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
    if fundamentals_context:
        payload["fundamentalsContext"] = fundamentals_context
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
        "depthReliable": req.depth_reliable,
        "marketRegime": req.market_regime,
    }
    if req.depth_reliable is False:
        fields["depthBid1Size"] = None
        fields["depthBid1MaxSize"] = None
        fields["depthQueueDropPct"] = None
    result = {key: value for key, value in fields.items() if value is not None}
    if result:
        result["schemaVersion"] = "technical-features-v1"
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Runtime kontroller
# ═══════════════════════════════════════════════════════════════════════════════


async def with_runtime_controls(
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
        return req, _static_risk_engine, False

    if mode_override is not None:
        req = req.model_copy(update={"mode": mode_override})
    return req, RiskEngine(runtime_config), kill_switch_enabled


def kill_switch_response(req: SignalRequest) -> SignalResponse:
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


async def with_resolved_daily_trade_count(req: SignalRequest) -> SignalRequest:
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


# ═══════════════════════════════════════════════════════════════════════════════
# AI yanıtı → RiskDecision
# ═══════════════════════════════════════════════════════════════════════════════


def _safe_action(raw_value: Any) -> SignalAction:
    """Parse action string safely — invalid values fall back to WAIT."""
    if not raw_value:
        return SignalAction.WAIT
    try:
        return SignalAction(str(raw_value).upper())
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


def dict_to_risk_decision(raw: dict, _req: SignalRequest | None = None) -> RiskDecision:
    """Parse a provider response dict into a RiskDecision.

    Every field is parsed defensively — no matter what garbage the AI
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


# ═══════════════════════════════════════════════════════════════════════════════
# Kalıcılık
# ═══════════════════════════════════════════════════════════════════════════════


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
                    position_qty=req.bot_position_qty,
                    total_account_qty=req.total_account_qty,
                    locked_long_term_qty=req.locked_long_term_qty,
                    mode=req.mode.value,
                )
            )
            session.add(
                AiDecisionModel(
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
                    entry_min=response.entry_range.min if response.entry_range else None,
                    entry_max=response.entry_range.max if response.entry_range else None,
                    stop_loss=response.stop_loss,
                    target_price=response.target_price,
                    order_type=response.order_type.value,
                    qty=response.qty,
                    mode=req.mode.value,
                )
            )
            await session.commit()

    except Exception:
        # DB is optional for the evaluation flow — never fail the caller
        logger.exception(
            "Failed to persist signal evaluation request_id=%s symbol=%s",
            req.request_id,
            req.symbol,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Gateway snapshot → SignalRequest
# ═══════════════════════════════════════════════════════════════════════════════


def _payload_get(payload: dict[str, Any], key: str, default: Any = None) -> Any:
    """Read a field from the snapshot payload, falling back to the nested
    ``technicalFeatures`` block (the gateway sends both flat and nested)."""
    if key in payload:
        return payload.get(key)
    nested = payload.get("technicalFeatures")
    if isinstance(nested, dict):
        return nested.get(key, default)
    return default


def snapshot_to_signal_request(
    symbol: str,
    payload: dict[str, Any],
    *,
    request_id: str,
    mode: SignalMode,
) -> SignalRequest:
    """Build a :class:`SignalRequest` from a gateway snapshot payload.

    ``dailyTradeCount`` is deliberately NOT set: leaving it out of
    ``model_fields_set`` lets :func:`with_resolved_daily_trade_count` fill it
    from ``order_logs``/``risk_decisions``. The gateway does not track the
    server's trade history, so its count would always read zero and silently
    disable the daily-limit gate.
    """
    return SignalRequest(
        requestId=request_id,
        symbol=symbol,
        timeframe=payload.get("timeframe", "1h"),
        lastPrice=payload.get("lastPrice", payload.get("close", 0)),
        open=payload.get("open", 0),
        high=payload.get("high", 0),
        low=payload.get("low", 0),
        volume=payload.get("volume", 0),
        ohlcReliable=payload.get("ohlcReliable"),
        ohlcSource=payload.get("ohlcSource"),
        quoteReliable=payload.get("quoteReliable"),
        priceSource=payload.get("priceSource"),
        rsi=payload.get("rsi") or payload.get("rsi14"),
        ema20=payload.get("ema20"),
        ema50=payload.get("ema50"),
        macd=payload.get("macd"),
        macdSignal=payload.get("macdSignal"),
        alphaTrendSignal=_payload_get(payload, "alphaTrendSignal"),
        alphaTrendMode=_payload_get(payload, "alphaTrendMode"),
        indicatorBuyCount=_payload_get(payload, "indicatorBuyCount"),
        indicatorSellCount=_payload_get(payload, "indicatorSellCount"),
        indicatorNeutralCount=_payload_get(payload, "indicatorNeutralCount"),
        indicatorConsensus=_payload_get(payload, "indicatorConsensus"),
        indicatorConsensusRatio=_payload_get(payload, "indicatorConsensusRatio"),
        atr=_payload_get(payload, "atr"),
        natr=_payload_get(payload, "natr"),
        adx=_payload_get(payload, "adx"),
        obvSlope=_payload_get(payload, "obvSlope"),
        vwapDistancePct=_payload_get(payload, "vwapDistancePct"),
        depthBid1Size=_payload_get(payload, "depthBid1Size"),
        depthBid1MaxSize=_payload_get(payload, "depthBid1MaxSize"),
        depthQueueDropPct=_payload_get(payload, "depthQueueDropPct"),
        depthReliable=_payload_get(payload, "depthReliable"),
        marketRegime=_payload_get(payload, "marketRegime"),
        botPositionQty=payload.get("botPositionQty", 0),
        totalAccountQty=payload.get("totalAccountQty", 0),
        lockedLongTermQty=payload.get("lockedLongTermQty", 0),
        mode=mode,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Ana değerlendirme akışı
# ═══════════════════════════════════════════════════════════════════════════════


def _build_request_id(symbol: str) -> str:
    """Eski bot'un BuildRequestId formatıyla uyumlu: SYMBOL-yyyyMMdd-HHmmss-scan."""
    return f"{symbol}-{datetime.now():%Y%m%d-%H%M%S}-scan"


def _snapshot_step(
    step_no: int, symbol: str, data_type: str, payload: dict[str, Any], reason: str
) -> dict[str, Any]:
    """AI payload'undaki ``agenticSteps`` girdisi — eski ContextStep şeması
    (stepNo/symbol/dataType/payload/reason) korunuyor ki prompt değişmesin."""
    return {
        "stepNo": step_no,
        "symbol": symbol,
        "dataType": data_type,
        "payload": payload,
        "reason": reason,
    }


async def evaluate_symbol(
    symbol: str,
    *,
    gateway: MatriksGatewayClient | None = None,
    provider: AiProvider | None = None,
    mode: SignalMode = SignalMode.PAPER,
    force_paper: bool = False,
    request_id: str | None = None,
) -> EvaluationResult | None:
    """Bir sembolü uçtan uca değerlendir; final kararı döndür.

    Args:
        symbol: Kök sembol (ör. ``"THYAO"``).
        gateway: Matriks gateway client'ı (default: paylaşılan singleton).
        provider: AI provider (default: settings'ten gelen singleton).
        mode: İstek modu — runtime ``tradingMode`` override'ı yine uygulanır.
        force_paper: True → mode override'dan SONRA bile PAPER'a sabitle;
            emir yolu bu çağrı için tamamen kapalı demektir.
        request_id: Verilmezse ``SYMBOL-yyyyMMdd-HHmmss-scan`` üretilir.

    Returns:
        ``EvaluationResult`` (final karar + efektif mod); veri
        değerlendirilemeyecek kadar bozuksa (lastPrice<=0) ``None``.

    Raises:
        GatewayUnavailable: Gateway'e hiç ulaşılamıyor — çağıran (scanner)
        yakalayıp turu atlar.
    """
    gateway = gateway or gateway_client
    symbol = symbol.strip().upper()
    request_id = request_id or _build_request_id(symbol)

    # ── 1. Kök sembol snapshot'ı ─────────────────────────────────────────
    snapshot = await gateway.get_snapshot(symbol)
    root_payload: dict[str, Any] = snapshot.get("payload") or {}

    last_price = root_payload.get("lastPrice") or 0
    if last_price <= 0:
        logger.warning(
            "Snapshot has no usable price; skipping evaluation symbol=%s "
            "priceSource=%s quoteReliable=%s",
            symbol,
            root_payload.get("priceSource"),
            root_payload.get("quoteReliable"),
        )
        return None

    steps: list[dict[str, Any]] = [
        _snapshot_step(1, symbol, "OHLCV", root_payload, "Root symbol snapshot")
    ]

    # ── 2. İlişkili sembol verisi ────────────────────────────────────────
    related = RELATED_SYMBOLS.get(symbol)
    if related is not None:
        try:
            related_snapshot = await gateway.get_snapshot(related)
            steps.append(
                _snapshot_step(
                    2,
                    related,
                    "DEPTH",
                    related_snapshot.get("payload") or {},
                    f"{symbol} için {related} derinlik verisi (ilişkili hisse)",
                )
            )
        except GatewayError as exc:
            # İlişkili veri "olsa iyi olur" kategorisi — yoksa kararı engellemez.
            logger.warning(
                "Related symbol snapshot failed; continuing without it "
                "root=%s related=%s error=%s",
                symbol,
                related,
                exc,
            )

    # ── 3. SignalRequest köprüsü ─────────────────────────────────────────
    sig_req = snapshot_to_signal_request(
        symbol, root_payload, request_id=request_id, mode=mode
    )

    # ── 4. Runtime kontroller ────────────────────────────────────────────
    sig_req, runtime_engine, kill_switch_enabled = await with_runtime_controls(sig_req)
    if force_paper and sig_req.mode != SignalMode.PAPER:
        sig_req = sig_req.model_copy(update={"mode": SignalMode.PAPER})

    if kill_switch_enabled:
        response = kill_switch_response(sig_req)
        payload = build_payload(sig_req, active_config=runtime_engine.config)
        raw = {
            "action": "WAIT",
            "confidence": 0.0,
            "risk_score": 0.0,
            "reason": response.reason,
        }
        _log_evaluation(sig_req, response)
        await persist_evaluation(sig_req, payload, raw, response)
        return EvaluationResult(response=response, mode=sig_req.mode)

    # ── 5. Dış bağlam (haber + akıllı para + admin fundamentals) ─────────
    news_context = await get_news_context([sig_req.symbol])
    broker_flow_context = await get_broker_flow_context([sig_req.symbol])
    fundamentals_context = await get_fundamentals_context([sig_req.symbol])

    payload = build_payload(
        sig_req,
        news_context=news_context,
        broker_flow_context=broker_flow_context,
        fundamentals_context=fundamentals_context,
        active_config=runtime_engine.config,
    )
    payload["agenticSteps"] = steps

    # ── 5.5. Pozisyon bağlamı (portfolio yönetimi) ───────────────────────
    # Açık bot pozisyonu varken LLM'in görevi yeni alım aramak değil eldeki
    # pozisyonu yönetmektir: maliyet + anlık K/Z payload'a eklenir ve prompt
    # kural 16 devreye girer (kar al / zarar kes / tut).
    position_context = await _build_position_context(sig_req)
    if position_context:
        payload["positionContext"] = position_context

    # ── 6. Admin test override VEYA AI kararı ────────────────────────────
    # Override asla REAL_LIVE'da uygulanmaz — test amaçlı bir özellik gerçek
    # sermayeyi hareket ettiremesin.
    raw: dict[str, Any] | None = None
    if sig_req.mode in (SignalMode.PAPER, SignalMode.MANUAL, SignalMode.DEMO_LIVE):
        try:
            async with async_session_factory() as ov_session:
                override = await consume_override(ov_session, sig_req.symbol)
            if override is not None:
                raw = override_to_raw_decision(override)
        except Exception:
            logger.exception("Failed to check signal override for %s", sig_req.symbol)

    # ── 6.5. Token-cost kapıları (LLM'e gitmeden karar) ──────────────────
    # Sıra: admin override > pre-flight gate > karar cache'i > LLM.
    if raw is None:
        gate_reason = preflight_wait_reason(
            symbol=sig_req.symbol,
            indicator_consensus=sig_req.indicator_consensus,
            bot_position_qty=sig_req.bot_position_qty,
            news_context=news_context,
        )
        if gate_reason is not None:
            raw = {
                "action": "WAIT",
                "confidence": 0.0,
                "risk_score": 0.0,
                "reason": gate_reason,
            }
            payload["decisionSource"] = "preflight-gate"

    if raw is None:
        cached = decision_cache.get(sig_req.symbol, sig_req.last_price, news_context)
        if cached is not None:
            raw = cached
            payload["decisionSource"] = "cache"

    if raw is None:
        provider = provider or get_default_provider()
        raw = await provider.decide(payload)
        payload["decisionSource"] = "llm"
        # Yalnızca gerçek LLM cevapları cache'lenir — kapı WAIT'leri değil.
        decision_cache.put(sig_req.symbol, sig_req.last_price, news_context, raw)

    # ── 7. RiskEngine (makro rejim filtresiyle) ──────────────────────────
    market_regime = await get_index_regime(gateway)
    decision = dict_to_risk_decision(raw, sig_req)
    sig_req = await with_resolved_daily_trade_count(sig_req)
    response = runtime_engine.evaluate(sig_req, decision, market_regime=market_regime)
    from app.services.news_risk_lock import apply_news_risk_lock
    response = await apply_news_risk_lock(response, sig_req.symbol)

    # ── 8. Log + persist ─────────────────────────────────────────────────
    _log_evaluation(sig_req, response)
    await persist_evaluation(sig_req, payload, raw, response)
    try:
        from app.services.position_management import record_position_management
        await record_position_management(sig_req, raw, response)
    except Exception:
        logger.exception("Position management persistence failed symbol=%s", sig_req.symbol)

    return EvaluationResult(response=response, mode=sig_req.mode)


async def _build_position_context(req: SignalRequest) -> dict[str, Any] | None:
    """Açık bot pozisyonu için maliyet + K/Z bağlamı üret; yoksa None.

    Maliyet ``bot_positions.avg_price``ten okunur (position_sync güncel
    tutar). DB hatası veya kayıt yokluğu evaluation'ı asla düşürmez.
    """
    if req.bot_position_qty <= 0:
        return None
    try:
        async with async_session_factory() as session:
            row = (
                await session.execute(
                    select(BotPositionModel).where(
                        BotPositionModel.symbol == req.symbol
                    )
                )
            ).scalar_one_or_none()
    except Exception:
        logger.exception("Position context load failed symbol=%s", req.symbol)
        row = None

    avg_cost = float(row.avg_price) if row is not None and row.avg_price else None
    context: dict[str, Any] = {
        "qty": req.bot_position_qty,
        "avgCost": avg_cost,
        "currentPrice": req.last_price,
        "positionValueTl": round(req.bot_position_qty * req.last_price, 2),
    }
    if avg_cost and avg_cost > 0 and req.last_price > 0:
        context["unrealizedPnlPct"] = round(
            (req.last_price - avg_cost) / avg_cost * 100, 2
        )
    return context


def _log_evaluation(req: SignalRequest, response: SignalResponse) -> None:
    log_signal_evaluation(
        request_id=req.request_id,
        symbol=req.symbol,
        mode=req.mode.value,
        request=req.model_dump(by_alias=True, exclude={"mode"}),
        response=response.model_dump(by_alias=True),
    )
