"""In-process signal evaluator â€” full-inversion mimarisinin beyni.

Eski FETCH_DATA/session protokolÃ¼nÃ¼n (``/api/signal/evaluate-agent`` +
``agent_session`` + ``session_store`` + ``agent_planner`` + HTTP ping-pong)
yerine geÃ§er: veri toplama artÄ±k aÄŸ Ã¼zerinden Ã§ok turlu bir oturum deÄŸil,
bu modÃ¼l iÃ§inde senkron gateway Ã§aÄŸrÄ±larÄ±dÄ±r.

AkÄ±ÅŸ::

    gateway.get_snapshot(root)                      # OHLCV+DEPTH+TECHNICAL tek Ã§aÄŸrÄ±da
      â””â”€ RELATED_SYMBOLS[root] varsa ek snapshot    # Ã¶r. ANELE â†’ THYAO derinliÄŸi
    â†’ SignalRequest kÃ¶prÃ¼sÃ¼
    â†’ runtime kontroller (kill switch, mode override, runtime risk config)
    â†’ news + fundamentals baÄŸlamÄ±
    â†’ admin test override VEYA AI provider
    â†’ RiskEngine
    â†’ log + DB persist (market_snapshots / ai_decisions / risk_decisions)

Bu modÃ¼l aynÄ± zamanda deÄŸerlendirme boru hattÄ±nÄ±n paylaÅŸÄ±lan yardÄ±mcÄ±larÄ±na
ev sahipliÄŸi yapar (``build_payload``, ``dict_to_risk_decision``,
``with_runtime_controls``, ``persist_evaluation`` â€¦). ``/api/signal/evaluate``
router'Ä± bunlarÄ± buradan alÄ±r â€” beyin serviste, HTTP katmanÄ± ince.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.config import AIProvider, settings
from app.core.logger import log_signal_evaluation
from app.core.risk_config import RiskConfig, risk_config
from app.db.session import async_session_factory
from sqlalchemy import select

from app.models.ai_decision_context import AiDecisionContext
from app.models.db import AiDecision as AiDecisionModel
from app.models.db import BotPosition as BotPositionModel
from app.models.db import OrderLog
from app.models.db import MarketSnapshot
from app.models.db import PositionSizingAudit
from app.models.db import RiskDecision as RiskDecisionModel
from app.models.db import TradeWatchlistSymbol
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
from app.services.account_context import (
    MatriksAccountContextAdapter,
    fetch_fresh_account_inputs,
    get_account_reservation_handling,
)
from app.services.cash_reservation import calculate_backend_reserved_cash
from app.services.daily_trade_count import get_today_trade_counts
from app.services.decision_gate import (
    decision_cache,
    decision_context_fingerprint,
    preflight_wait_reason,
)
from app.services.fundamentals_service import get_fundamentals_context
from app.services.effective_risk_config import (
    EffectiveRiskConfigResolver,
    EnvironmentRiskLimits,
    SystemRiskConfig,
    resolve_effective_risk_config,
)
from app.services.market_regime import get_index_regime
from app.services.market_data_contract import normalize_snapshot_payload
from app.services.matriks_gateway import (
    GatewayError,
    MatriksGatewayClient,
    gateway_client,
)
from app.services.news_service import get_news_context
from app.services.kap_service import get_kap_context
from app.services.risk_engine import RiskDecision, RiskEngine
from app.services.signal_override import consume_override, override_to_raw_decision
from app.services.trade_profile import get_active_profile, get_static_default_profile

logger = logging.getLogger(__name__)


def _json_safe(value: Any) -> Any:
    """Return a JSON-compatible copy for DB JSON columns."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))

# Statik singleton â€” runtime config yÃ¼klenemediÄŸinde kullanÄ±lan yedek motor.
_static_effective_config = EffectiveRiskConfigResolver().resolve(
    environment_limits=EnvironmentRiskLimits.from_environment(),
    system_config=SystemRiskConfig(),
    trade_profile=get_static_default_profile(),
)
_static_risk_engine = RiskEngine(risk_config, _static_effective_config)


def _decision_persistence_metadata(payload: dict[str, Any]) -> tuple[str, str | None]:
    """Label AI-decision rows accurately without claiming a model was called."""
    source = str(payload.get("decisionSource") or "system-gate")
    if source != "llm":
        return source, None
    model = (
        settings.deepseek_model if settings.ai_provider == AIProvider.DEEPSEEK else None
    )
    return settings.ai_provider.value, model


# KÃ¶k sembol deÄŸerlendirilirken derinliÄŸi de Ã§ekilen iliÅŸkili hisseler.
# (Eski agent_planner.RELATED_SYMBOLS â€” planner silindi, kural burada yaÅŸÄ±yor.)
RELATED_SYMBOLS: dict[str, str] = {
    "ANELE": "THYAO",
    "PGSUS": "THYAO",
    "TUPRS": "KCHOL",
}


@dataclass(frozen=True)
class EvaluationResult:
    """Final karar + kararÄ±n alÄ±ndÄ±ÄŸÄ± efektif mod.

    ``mode`` runtime override'lar ve ``force_paper`` uygulandÄ±ktan sonraki
    deÄŸerdir â€” scanner'Ä±n emir gÃ¶nderme kapÄ±sÄ± bu alana bakar (SignalResponse
    mode taÅŸÄ±maz).
    """

    response: SignalResponse
    mode: SignalMode
    decision_created_utc: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    evaluation_purpose: str = "TRADING"
    research_score: float | None = None
    raw_action: SignalAction | None = None


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# AI payload oluÅŸturma
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•


def build_payload(
    req: SignalRequest,
    news_context: dict[str, Any] | None = None,
    fund_context: dict[str, Any] | None = None,
    broker_flow_context: dict[str, Any] | None = None,
    fundamentals_context: dict[str, Any] | None = None,
    kap_context: dict[str, Any] | None = None,
    active_config: RiskConfig | None = None,
) -> dict:
    """Build the complete audit payload retained outside provider requests.

    ``news_context``, ``broker_flow_context`` (smart-money / AKD flow) and
    ``fundamentals_context`` are live: the scanner/evaluate flow fetches them
    per symbol and passes them here. ``fund_context`` (fund_scanner) is still
    a placeholder and normally not passed â€” kept in the signature so wiring a
    real source later is a one-line change at the call site.
    """
    config = active_config or risk_config
    payload = {
        "schemaVersion": "technical-features-v2",
        "deprecatedFields": ["volume", "timeframe", "marketRegime", "dailyTradeCount"],
        "symbol": req.symbol,
        "timeframe": req.timeframe,
        "requestedTimeframe": req.requested_timeframe,
        "actualBarPeriod": req.actual_bar_period,
        "actualBarPeriodSeconds": req.actual_bar_period_seconds,
        "barPeriodSource": req.bar_period_source,
        "timeframeMismatch": req.timeframe_mismatch,
        "indicatorPeriod": req.indicator_period,
        "indicatorPeriodSeconds": req.indicator_period_seconds,
        "instrumentType": req.instrument_type,
        "lastPrice": req.last_price,
        "open": req.open,
        "high": req.high,
        "low": req.low,
        "volume": req.volume,
        "barVolume": req.bar_volume,
        "barVolumeSource": req.bar_volume_source,
        "barVolumeUnit": req.bar_volume_unit,
        "barVolumeReliable": req.bar_volume_reliable,
        "sessionTurnoverTl": req.session_turnover_tl,
        "totalVol": req.total_vol,
        "totalVolSource": req.total_vol_source,
        "totalVolUnit": req.total_vol_unit,
        "totalVolReliable": req.total_vol_reliable,
        "volumeIndicatorValue": req.volume_indicator_value,
        "volumeTlIndicatorValue": req.volume_tl_indicator_value,
        "barClosed": req.bar_closed,
        "barIsNew": req.bar_is_new,
        "barDataIndex": req.bar_data_index,
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
        "accountAvailableQty": req.account_available_qty,
        "lockedLongTermQty": req.locked_long_term_qty,
        "tradeEligible": req.trade_eligible,
        "evaluationPurpose": req.evaluation_purpose,
        "lastTradeUtc": req.last_trade_utc,
        "quoteReadUtc": req.quote_read_utc,
        "depthReadUtc": req.depth_read_utc,
        "barEventUtc": req.bar_event_utc,
        "quoteTimestampSource": req.quote_timestamp_source,
        "depthTimestampSource": req.depth_timestamp_source,
        "depthEventTimestampAvailable": req.depth_event_timestamp_available,
        "depthReadLatencySeconds": req.depth_read_latency_seconds,
        "barTimestampSource": req.bar_timestamp_source,
        "barTimeReliable": req.bar_time_reliable,
        "barTimestampFallbackObservationUtc": (
            req.bar_timestamp_fallback_observation_utc
        ),
        "barTimestampFallbackObservationSource": (
            req.bar_timestamp_fallback_observation_source
        ),
        "snapshotBuiltUtc": req.snapshot_built_utc,
        "depthSummary": req.depth_summary,
        "dailyAcceptedOrderCount": req.daily_accepted_order_count,
        "dailyFilledOrderCount": req.daily_filled_order_count,
        "allowedSymbols": sorted(config._allowed_set()),
        "declinedSymbols": sorted(config._declined_set()),
        "lockedSymbols": sorted(config._locked_set()),
    }
    technical_features = _build_technical_feature_payload(req)
    if technical_features:
        payload["technicalFeatures"] = technical_features
    if news_context:
        payload["newsContext"] = news_context
    if fund_context:
        payload["fundContext"] = fund_context
    if broker_flow_context:
        payload["brokerFlowContext"] = broker_flow_context
    if fundamentals_context:
        payload["fundamentalsContext"] = fundamentals_context
    if kap_context:
        payload["kapContext"] = kap_context
    depth_context = _build_depth_context(req)
    if depth_context:
        payload["depthContext"] = depth_context
    return payload




def build_ai_decision_context(
    req: SignalRequest,
    *,
    news_context: dict[str, Any] | None = None,
    broker_flow_context: dict[str, Any] | None = None,
    kap_context: dict[str, Any] | None = None,
    profile: str | None = None,
    macro_market_regime: str | None = None,
    position_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the only market-data contract sent to an AI provider.

    The gateway snapshot and the broader audit payload deliberately stay out of
    this contract. ``exclude_none=True`` keeps it compact without dropping
    meaningful zeroes or ``False`` flags.
    """
    symbol = req.symbol.strip().upper()
    news_entry = (news_context or {}).get(symbol, {})
    raw_news = news_entry.get("latestNews", []) if isinstance(news_entry, dict) else []
    news_items = []
    for item in raw_news[:3] if isinstance(raw_news, list) else []:
        if not isinstance(item, dict):
            continue
        headline = str(item.get("title") or "").strip()
        if not headline:
            continue
        summary = item.get("summary")
        news_items.append(
            {
                "headline": headline[:500],
                "summary": str(summary)[:1000] if summary else None,
                "sentiment": item.get("sentiment") or "UNKNOWN",
            }
        )

    broker_entry = (broker_flow_context or {}).get(symbol, {})
    kap_entry = (kap_context or {}).get(symbol, {})
    events: dict[str, Any] = {}
    if news_items:
        events["news"] = {"items": news_items}
    if isinstance(broker_entry, dict) and broker_entry:
        top_buyers = broker_entry.get("topBuyers") or []
        top_sellers = broker_entry.get("topSellers") or []
        events["brokerFlow"] = {
            "smartMoneyFlow": broker_entry.get("smartMoneyFlow"),
            "netSmartLot": broker_entry.get("netSmartLot"),
            "topBuyer": (top_buyers[0].get("name") if top_buyers and isinstance(top_buyers[0], dict) else None),
            "topSeller": (top_sellers[0].get("name") if top_sellers and isinstance(top_sellers[0], dict) else None),
        }
    if isinstance(kap_entry, dict) and kap_entry:
        risk_events = kap_entry.get("riskEvents24h") or []
        events["kap"] = {
            "blockingRisk": kap_entry.get("hasBlockingRisk"),
            "activeRiskCount": len(risk_events) if isinstance(risk_events, list) else None,
            "unknownDateRisk": kap_entry.get("hasUnknownDateRisk"),
        }

    depth: dict[str, Any] | None = None
    if req.depth_reliable is not None:
        depth = {"reliable": req.depth_reliable}
        if req.depth_reliable is False:
            depth["signal"] = "UNAVAILABLE"
        else:
            depth.update({
                "spreadPct": req.spread_pct,
                "buyPressure": min(
                    1.0, max(0.0, (req.depth_buy_pressure_score or 0.0) / 100.0)
                ),
                "signal": req.depth_order_book_signal,
                "bidAskRatio": req.depth_bid_ask_ratio_top5,
                "nearestBidWallDistancePct": req.depth_nearest_bid_wall_distance_pct,
                "nearestAskWallDistancePct": req.depth_nearest_ask_wall_distance_pct,
                "wallConcentrationRisk": req.depth_wall_concentration_risk,
            })

    position: dict[str, Any] | None = None
    if req.bot_position_qty > 0:
        position = {
            "botQty": req.bot_position_qty,
            "botAvgCost": (position_context or {}).get("botAvgCost"),
            "unrealizedPnlPct": (position_context or {}).get("botUnrealizedPnlPct"),
            "lockedLongTerm": req.locked_long_term_qty > 0,
        }

    context = AiDecisionContext.model_validate({
        "symbol": symbol,
        "period": {"requested": req.requested_timeframe or req.timeframe, "actual": req.actual_bar_period, "mismatch": req.timeframe_mismatch},
        "profile": profile,
        "evaluationPurpose": req.evaluation_purpose,
        "dataQuality": {"quoteAgeSec": req.quote_age_seconds, "ohlcvAgeSec": req.ohlcv_age_seconds, "depthAgeSec": req.depth_age_seconds, "quoteReliable": req.quote_reliable, "ohlcReliable": req.ohlc_reliable},
        "price": {"last": req.last_price, "open": req.open, "high": req.high, "low": req.low},
        "market": {"barVolume": req.bar_volume, "sessionTurnoverTl": req.session_turnover_tl, "macroMarketRegime": macro_market_regime, "symbolTrendRegime": req.symbol_trend_regime or req.market_regime},
        "technical": {
            "rsi": req.rsi, "ema20": req.ema20, "ema50": req.ema50,
            "macd": req.macd, "macdSignal": req.macd_signal, "atr": req.atr,
            "natr": req.natr, "adx": req.adx, "obvSlope": req.obv_slope,
            "vwapDistancePct": req.vwap_distance_pct, "alphaTrendSignal": req.alpha_trend_signal,
            "indicatorConsensus": req.indicator_consensus, "indicatorConsensusRatio": req.indicator_consensus_ratio,
            "indicatorBuyCount": req.indicator_buy_count, "indicatorSellCount": req.indicator_sell_count,
            "indicatorNeutralCount": req.indicator_neutral_count,
        },
        "depth": depth,
        "position": position,
        "events": events or None,
    })
    return context.model_dump(exclude_none=True)

def _build_depth_context(req: SignalRequest) -> dict[str, Any]:
    if req.depth_reliable is False:
        return {"depthReliable": False, "orderBookSignal": "UNAVAILABLE"}
    fields = {
        "levelsUsed": req.depth_levels_used,
        "spreadPct": req.spread_pct,
        "bidAskRatioTop5": req.depth_bid_ask_ratio_top5,
        "bidAskRatioTop10": req.depth_bid_ask_ratio_top10,
        "bidAskRatioTop25": req.depth_bid_ask_ratio_top25,
        "imbalanceTop5": req.depth_imbalance_top5,
        "imbalanceTop10": req.depth_imbalance_top10,
        "imbalanceTop25": req.depth_imbalance_top25,
        "bidConcentrationTop3Pct": req.depth_bid_concentration_top3_pct,
        "askConcentrationTop3Pct": req.depth_ask_concentration_top3_pct,
        "largestBidWallDistancePct": req.depth_largest_bid_wall_distance_pct,
        "largestAskWallDistancePct": req.depth_largest_ask_wall_distance_pct,
        "nearestBidWallDistancePct": req.depth_nearest_bid_wall_distance_pct,
        "nearestAskWallDistancePct": req.depth_nearest_ask_wall_distance_pct,
        "buyPressureScore": req.depth_buy_pressure_score,
        "sellPressureScore": req.depth_sell_pressure_score,
        "orderBookSignal": req.depth_order_book_signal,
        "wallConcentrationRisk": req.depth_wall_concentration_risk,
        "depthAgeSeconds": req.depth_age_seconds,
        "depthReliable": req.depth_reliable,
    }
    return {key: value for key, value in fields.items() if value is not None}


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
        "atrPeriod": req.atr_period,
        "atrTimeframe": req.atr_timeframe,
        "volatilityMetricSource": req.volatility_metric_source,
        "closeChangeVolatilityProxy": req.close_change_volatility_proxy,
        "adx": req.adx,
        "obvSlope": req.obv_slope,
        "vwapDistancePct": req.vwap_distance_pct,
        "depthBid1Size": req.depth_bid1_size,
        "depthBid1MaxSize": req.depth_bid1_max_size,
        "depthQueueDropPct": req.depth_queue_drop_pct,
        "depthReliable": req.depth_reliable,
        "symbolTrendRegime": req.symbol_trend_regime or req.market_regime,
    }
    if req.depth_reliable is False:
        fields["depthBid1Size"] = None
        fields["depthBid1MaxSize"] = None
        fields["depthQueueDropPct"] = None
    result = {key: value for key, value in fields.items() if value is not None}
    if result:
        result["schemaVersion"] = "technical-features-v2"
    return result


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Runtime kontroller
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•


async def with_runtime_controls(
    req: SignalRequest,
) -> tuple[SignalRequest, RiskEngine, bool]:
    """Apply DB-backed runtime config controls when available."""
    try:
        async with async_session_factory() as session:
            runtime_config = await build_runtime_risk_config(session)
            mode_override = await get_trading_mode_override(session)
            kill_switch_enabled = await is_kill_switch_enabled(session)
            effective_config = await resolve_effective_risk_config(session)
    except Exception:
        logger.exception(
            "Failed to load runtime admin config request_id=%s symbol=%s",
            req.request_id,
            req.symbol,
        )
        return req, _static_risk_engine, False

    if mode_override is not None:
        req = req.model_copy(update={"mode": mode_override})
    return req, RiskEngine(runtime_config, effective_config), kill_switch_enabled


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
    return req.model_copy(
        update={
            # Legacy risk-engine input remains the conservative de-duplicated
            # request count. Explicit v2 fields state what they actually count.
            "daily_trade_count": counts.effective_count,
            "daily_accepted_order_count": counts.symbol_accepted_order_count,
            "daily_filled_order_count": counts.symbol_filled_order_count,
        }
    )


async def with_trade_eligibility(req: SignalRequest) -> SignalRequest:
    """Resolve the DB-backed BUY gate; any DB problem remains fail-closed."""
    try:
        async with async_session_factory() as session:
            now = datetime.now(timezone.utc)
            eligible = (
                await session.execute(
                    select(TradeWatchlistSymbol.id).where(
                        TradeWatchlistSymbol.symbol == req.symbol.strip().upper(),
                        TradeWatchlistSymbol.is_active.is_(True),
                        (TradeWatchlistSymbol.expires_at.is_(None))
                        | (TradeWatchlistSymbol.expires_at >= now),
                    )
                )
            ).scalar_one_or_none()
    except Exception:
        logger.exception(
            "Trade eligibility unavailable; BUY remains blocked request_id=%s symbol=%s",
            req.request_id,
            req.symbol,
        )
        eligible = None
    return req.model_copy(update={"trade_eligible": eligible is not None})


async def with_fresh_account_sizing_context(
    req: SignalRequest,
    *,
    gateway: MatriksGatewayClient,
    snapshot: dict[str, Any],
    runtime_engine: RiskEngine,
) -> SignalRequest:
    """Attach normalized account data for an AI BUY, otherwise fail closed."""
    effective = runtime_engine.effective_config
    if effective is None:
        return req
    try:
        inputs = await fetch_fresh_account_inputs(
            gateway, symbol=req.symbol, target_snapshot=snapshot
        )
        async with async_session_factory() as session:
            reserved = await calculate_backend_reserved_cash(session)
            handling = await get_account_reservation_handling(session)
            adapter = MatriksAccountContextAdapter(
                reservation_handling=handling,
                allow_margin_buying=effective.allow_margin_buying,
                max_account_data_age_seconds=effective.max_account_data_age_seconds,
            )
            context = adapter.normalize(
                raw_account=inputs.raw_account,
                raw_positions=inputs.raw_positions,
                raw_open_orders=inputs.raw_open_orders,
                backend_reserved_cash_tl=reserved,
                symbol=req.symbol,
                market_prices=inputs.market_prices,
            )
            await adapter.add_audit(
                session, request_id=req.request_id, symbol=req.symbol
            )
            await session.commit()
        return req.model_copy(update={"account_sizing_context": context})
    except Exception:
        logger.exception(
            "Fresh account normalization failed; BUY remains blocked request_id=%s",
            req.request_id,
        )
        return req


def _has_explicit_daily_trade_count(req: SignalRequest) -> bool:
    """Return True when dailyTradeCount was present in the request payload."""
    return bool({"daily_trade_count", "dailyTradeCount"} & req.model_fields_set)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# AI yanÄ±tÄ± â†’ RiskDecision
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•


def _safe_action(raw_value: Any) -> SignalAction:
    """Parse action string safely â€” invalid values fall back to WAIT."""
    if not raw_value:
        return SignalAction.WAIT
    try:
        return SignalAction(str(raw_value).upper())
    except ValueError:
        return SignalAction.WAIT


def _safe_float(raw_value: Any, default: Any = 0.0) -> Any:
    """Parse a float safely â€” non-numeric values return the default."""
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except (ValueError, TypeError):
        return default


def _safe_decimal(raw_value: Any, default: Any = None) -> Decimal | Any:
    """Parse an external financial value without Decimal(float)."""
    if raw_value is None:
        return default
    try:
        value = raw_value if isinstance(raw_value, Decimal) else Decimal(str(raw_value))
    except (InvalidOperation, ValueError, TypeError):
        return default
    return value if value.is_finite() else default


def dict_to_risk_decision(raw: dict, _req: SignalRequest | None = None) -> RiskDecision:
    """Parse a provider response dict into a RiskDecision.

    Every field is parsed defensively â€” no matter what garbage the AI
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

    Never raises â€” garbage values produce None.
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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# KalÄ±cÄ±lÄ±k
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•


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
                    mode=req.mode.value,
                )
            )
            # Cache tekrarÄ±nda saÄŸlayÄ±cÄ±nÄ±n eski gecikmesini yazmak yanÄ±ltÄ±cÄ±
            # olur â€” sÃ¼re yalnÄ±zca gerÃ§ek LLM Ã§aÄŸrÄ±sÄ±nda kaydedilir.
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
                    mode=req.mode.value,
                )
            )
            await session.commit()

    except Exception:
        # DB is optional for the evaluation flow â€” never fail the caller
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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Gateway snapshot â†’ SignalRequest
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•


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
    payload = normalize_snapshot_payload(payload)
    depth = (
        payload.get("depthAnalysis")
        if isinstance(payload.get("depthAnalysis"), dict)
        else {}
    )
    largest_bid = depth.get("largestBidWall") or {}
    largest_ask = depth.get("largestAskWall") or {}
    nearest_bid = depth.get("nearestLargeBidWall") or {}
    nearest_ask = depth.get("nearestLargeAskWall") or {}
    return SignalRequest(
        requestId=request_id,
        symbol=symbol,
        timeframe=payload.get("timeframe", "UNKNOWN"),
        requestedTimeframe=payload.get("requestedTimeframe"),
        actualBarPeriod=payload.get("actualBarPeriod"),
        actualBarPeriodSeconds=payload.get("actualBarPeriodSeconds"),
        barPeriodSource=payload.get("barPeriodSource"),
        timeframeMismatch=payload.get("timeframeMismatch", False),
        indicatorPeriod=payload.get("indicatorPeriod"),
        indicatorPeriodSeconds=payload.get("indicatorPeriodSeconds"),
        instrumentType=payload.get("instrumentType"),
        lastPrice=payload.get("lastPrice", payload.get("close", 0)),
        open=payload.get("open", 0),
        high=payload.get("high", 0),
        low=payload.get("low", 0),
        volume=payload.get("volume", 0),
        barVolume=payload.get("barVolume"),
        barVolumeSource=payload.get("barVolumeSource"),
        barVolumeUnit=payload.get("barVolumeUnit"),
        barVolumeReliable=payload.get("barVolumeReliable"),
        sessionTurnoverTl=payload.get("sessionTurnoverTl"),
        totalVol=payload.get("totalVol"),
        totalVolSource=payload.get("totalVolSource"),
        totalVolUnit=payload.get("totalVolUnit"),
        totalVolReliable=payload.get("totalVolReliable"),
        volumeIndicatorValue=payload.get("volumeIndicatorValue"),
        volumeTlIndicatorValue=payload.get("volumeTlIndicatorValue"),
        barClosed=payload.get("barClosed"),
        barIsNew=payload.get("barIsNew"),
        barDataIndex=payload.get("barDataIndex"),
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
        atrPeriod=_payload_get(payload, "atrPeriod"),
        atrTimeframe=_payload_get(payload, "atrTimeframe"),
        volatilityMetricSource=_payload_get(payload, "volatilityMetricSource"),
        closeChangeVolatilityProxy=_payload_get(
            payload, "closeChangeVolatilityProxy"
        ),
        adx=_payload_get(payload, "adx"),
        obvSlope=_payload_get(payload, "obvSlope"),
        vwapDistancePct=_payload_get(payload, "vwapDistancePct"),
        depthBid1Size=_payload_get(payload, "depthBid1Size"),
        depthBid1MaxSize=_payload_get(payload, "depthBid1MaxSize"),
        depthQueueDropPct=_payload_get(payload, "depthQueueDropPct"),
        depthReliable=depth.get(
            "depthReliable", _payload_get(payload, "depthReliable")
        ),
        depthLevelsUsed=depth.get("levelsUsed"),
        spreadPct=depth.get("spreadPct"),
        depthBidAskRatioTop5=depth.get("bidAskRatioTop5"),
        depthBidAskRatioTop10=depth.get("bidAskRatioTop10"),
        depthBidAskRatioTop25=depth.get("bidAskRatioTop25"),
        depthImbalanceTop5=depth.get("imbalanceTop5"),
        depthImbalanceTop10=depth.get("imbalanceTop10"),
        depthImbalanceTop25=depth.get("imbalanceTop25"),
        depthBidConcentrationTop3Pct=depth.get("bidConcentrationTop3Pct"),
        depthAskConcentrationTop3Pct=depth.get("askConcentrationTop3Pct"),
        depthLargestBidWallDistancePct=largest_bid.get("distancePct"),
        depthLargestAskWallDistancePct=largest_ask.get("distancePct"),
        depthNearestBidWallDistancePct=nearest_bid.get("distancePct"),
        depthNearestAskWallDistancePct=nearest_ask.get("distancePct"),
        depthBuyPressureScore=depth.get("buyPressureScore"),
        depthSellPressureScore=depth.get("sellPressureScore"),
        depthOrderBookSignal=depth.get("orderBookSignal"),
        depthSummary=payload.get("depthSummary"),
        depthWallConcentrationRisk=(
            bool(
                depth.get("bidWallConcentrationRisk")
                or depth.get("askWallConcentrationRisk")
            )
            if depth
            else None
        ),
        quoteAgeSeconds=payload.get("quoteAgeSeconds"),
        ohlcvAgeSeconds=payload.get("ohlcvAgeSeconds"),
        depthAgeSeconds=payload.get("depthAgeSeconds"),
        lastTradeUtc=payload.get("lastTradeUtc") or payload.get("quoteEventUtc"),
        quoteReadUtc=payload.get("quoteReadUtc"),
        depthReadUtc=payload.get("depthReadUtc"),
        barEventUtc=payload.get("barEventUtc"),
        quoteTimestampSource=payload.get("quoteTimestampSource"),
        depthTimestampSource=payload.get("depthTimestampSource"),
        depthEventTimestampAvailable=payload.get("depthEventTimestampAvailable"),
        depthReadLatencySeconds=payload.get("depthReadLatencySeconds"),
        barTimestampSource=payload.get("barTimestampSource"),
        barTimeReliable=payload.get("barTimeReliable"),
        barTimestampFallbackObservationUtc=payload.get(
            "barTimestampFallbackObservationUtc"
        ),
        barTimestampFallbackObservationSource=payload.get(
            "barTimestampFallbackObservationSource"
        ),
        snapshotBuiltUtc=payload.get("snapshotBuiltUtc"),
        symbolTrendRegime=(
            _payload_get(payload, "symbolTrendRegime")
            or _payload_get(payload, "marketRegime")
        ),
        botPositionQty=payload.get("botPositionQty", 0),
        totalAccountQty=payload.get("totalAccountQty", 0),
        accountAvailableQty=payload.get("accountAvailableQty", 0),
        lockedLongTermQty=payload.get("lockedLongTermQty", 0),
        positionContext=(
            payload.get("positionContext")
            if isinstance(payload.get("positionContext"), dict)
            else None
        ),
        mode=mode,
    )


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Ana deÄŸerlendirme akÄ±ÅŸÄ±
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•


def _build_request_id(symbol: str) -> str:
    """Eski bot'un BuildRequestId formatÄ±yla uyumlu: SYMBOL-yyyyMMdd-HHmmss-scan."""
    return f"{symbol}-{datetime.now():%Y%m%d-%H%M%S}-scan"


def _snapshot_step(
    step_no: int, symbol: str, data_type: str, payload: dict[str, Any], reason: str
) -> dict[str, Any]:
    """AI payload'undaki ``agenticSteps`` girdisi â€” eski ContextStep ÅŸemasÄ±
    (stepNo/symbol/dataType/payload/reason) korunuyor ki prompt deÄŸiÅŸmesin."""
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
    evaluation_purpose: str = "TRADING",
    research_context: dict[str, Any] | None = None,
) -> EvaluationResult | None:
    """Bir sembolÃ¼ uÃ§tan uca deÄŸerlendir; final kararÄ± dÃ¶ndÃ¼r.

    Args:
        symbol: KÃ¶k sembol (Ã¶r. ``"THYAO"``).
        gateway: Matriks gateway client'Ä± (default: paylaÅŸÄ±lan singleton).
        provider: AI provider (default: settings'ten gelen singleton).
        mode: Ä°stek modu â€” runtime ``tradingMode`` override'Ä± yine uygulanÄ±r.
        force_paper: True â†’ mode override'dan SONRA bile PAPER'a sabitle;
            emir yolu bu Ã§aÄŸrÄ± iÃ§in tamamen kapalÄ± demektir.
        request_id: Verilmezse ``SYMBOL-yyyyMMdd-HHmmss-scan`` Ã¼retilir.

    Returns:
        ``EvaluationResult`` (final karar + efektif mod); veri
        deÄŸerlendirilemeyecek kadar bozuksa (lastPrice<=0) ``None``.

    Raises:
        GatewayUnavailable: Gateway'e hiÃ§ ulaÅŸÄ±lamÄ±yor â€” Ã§aÄŸÄ±ran (scanner)
        yakalayÄ±p turu atlar.
    """
    gateway = gateway or gateway_client
    decision_created_utc = datetime.now(timezone.utc)
    symbol = symbol.strip().upper()
    request_id = request_id or _build_request_id(symbol)
    evaluation_purpose = str(evaluation_purpose or "TRADING").strip().upper()
    research_only = evaluation_purpose == "RESEARCH_DISCOVERY"

    # â”€â”€ 1. KÃ¶k sembol snapshot'Ä± â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

    # â”€â”€ 2. Ä°liÅŸkili sembol verisi â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
                    f"{symbol} iÃ§in {related} derinlik verisi (iliÅŸkili hisse)",
                )
            )
        except GatewayError as exc:
            # Ä°liÅŸkili veri "olsa iyi olur" kategorisi â€” yoksa kararÄ± engellemez.
            logger.warning(
                "Related symbol snapshot failed; continuing without it "
                "root=%s related=%s error=%s",
                symbol,
                related,
                exc,
            )

    # â”€â”€ 3. SignalRequest kÃ¶prÃ¼sÃ¼ â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    sig_req = snapshot_to_signal_request(
        symbol, root_payload, request_id=request_id, mode=mode
    )


    sig_req = sig_req.model_copy(
        update={"evaluation_purpose": evaluation_purpose}
    )

    # â”€â”€ 4. Runtime kontroller â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    sig_req, runtime_engine, kill_switch_enabled = await with_runtime_controls(sig_req)
    sig_req = await with_resolved_daily_trade_count(sig_req)
    sig_req = await with_trade_eligibility(sig_req)
    if (force_paper or research_only) and sig_req.mode != SignalMode.PAPER:
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
        return EvaluationResult(
            response=response,
            mode=sig_req.mode,
            decision_created_utc=decision_created_utc,
            evaluation_purpose=evaluation_purpose,
            raw_action=SignalAction.WAIT,
        )

    # â”€â”€ 5. DÄ±ÅŸ baÄŸlam (haber + akÄ±llÄ± para + admin fundamentals) â”€â”€â”€â”€â”€â”€â”€â”€â”€
    runtime_config_hash = decision_context_fingerprint(
        runtime_engine.config.model_dump(mode="json")
    )
    async with async_session_factory() as profile_session:
        active_profile_code = (await get_active_profile(profile_session)).code
    try:
        (
            news_context,
            kap_context,
            broker_flow_context,
            fundamentals_context,
            market_regime,
        ) = await asyncio.wait_for(
            asyncio.gather(
                get_news_context([sig_req.symbol]),
                get_kap_context([sig_req.symbol]),
                get_broker_flow_context(
                    [sig_req.symbol], config_version=runtime_config_hash
                ),
                get_fundamentals_context([sig_req.symbol]),
                get_index_regime(gateway),
            ),
            timeout=12.0,
        )
    except asyncio.TimeoutError:
        logger.warning("Context budget exceeded symbol=%s", sig_req.symbol)
        news_context, kap_context, fundamentals_context, market_regime = (
            {},
            {},
            {},
            None,
        )
        broker_flow_context = {
            sig_req.symbol: {"available": False, "smartMoneyFlow": "UNKNOWN"}
        }

    payload = build_payload(
        sig_req,
        news_context=news_context,
        broker_flow_context=broker_flow_context,
        fundamentals_context=fundamentals_context,
        kap_context=kap_context,
        active_config=runtime_engine.config,
    )
    payload["agenticSteps"] = steps
    payload["macroMarketRegime"] = market_regime
    payload["macroMarketRegimeSymbol"] = settings.market_index_symbol.strip().upper()
    payload["symbolTrendRegime"] = sig_req.symbol_trend_regime
    sig_req = sig_req.model_copy(
        update={
            "macro_market_regime": market_regime,
            "macro_market_regime_symbol": settings.market_index_symbol.strip().upper(),
        }
    )
    payload["runtimeMode"] = sig_req.mode.value
    payload["configHash"] = runtime_config_hash
    payload["profileCode"] = active_profile_code
    if research_context:
        payload.update(_json_safe(research_context))
    payload["evaluationPurpose"] = evaluation_purpose
    if research_only:
        payload["allowOrder"] = False

    # â”€â”€ 5.5. Pozisyon baÄŸlamÄ± (portfolio yÃ¶netimi) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # AÃ§Ä±k bot pozisyonu varken LLM'in gÃ¶revi yeni alÄ±m aramak deÄŸil eldeki
    # pozisyonu yÃ¶netmektir: maliyet + anlÄ±k K/Z payload'a eklenir ve prompt
    # kural 16 devreye girer (kar al / zarar kes / tut).
    position_context = await _build_position_context(sig_req)
    if position_context:
        payload["positionContext"] = position_context

    # â”€â”€ 6. Admin test override VEYA AI kararÄ± â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Override asla REAL_LIVE'da uygulanmaz â€” test amaÃ§lÄ± bir Ã¶zellik gerÃ§ek
    # sermayeyi hareket ettiremesin.
    ai_context = build_ai_decision_context(
        sig_req,
        news_context=news_context,
        broker_flow_context=broker_flow_context,
        kap_context=kap_context,
        profile=active_profile_code,
        macro_market_regime=market_regime,
        position_context=position_context,
    )
    raw: dict[str, Any] | None = None
    if not research_only and sig_req.mode in (
        SignalMode.PAPER,
        SignalMode.MANUAL,
        SignalMode.DEMO_LIVE,
    ):
        try:
            async with async_session_factory() as ov_session:
                override = await consume_override(ov_session, sig_req.symbol)
            if override is not None:
                raw = override_to_raw_decision(override)
        except Exception:
            logger.exception("Failed to check signal override for %s", sig_req.symbol)

    # â”€â”€ 6.5. Token-cost kapÄ±larÄ± (LLM'e gitmeden karar) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # SÄ±ra: admin override > pre-flight gate > karar cache'i > LLM.
    if raw is None and not research_only:
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
        context_fingerprint = decision_context_fingerprint(ai_context)
        cached = decision_cache.get(
            sig_req.symbol, sig_req.last_price, news_context, context_fingerprint
        )
        if cached is not None:
            raw = cached
            payload["decisionSource"] = "cache"

    if raw is None:
        provider = provider or get_default_provider()
        raw = await provider.decide(ai_context)
        payload["decisionSource"] = "llm"
        # YalnÄ±zca gerÃ§ek LLM cevaplarÄ± cache'lenir â€” kapÄ± WAIT'leri deÄŸil.
        decision_cache.put(
            sig_req.symbol, sig_req.last_price, news_context, raw, context_fingerprint
        )

    # â”€â”€ 7. RiskEngine (makro rejim filtresiyle) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    decision = dict_to_risk_decision(raw, sig_req)
    if decision.action == SignalAction.BUY and not research_only:
        sig_req = await with_fresh_account_sizing_context(
            sig_req,
            gateway=gateway,
            snapshot=snapshot,
            runtime_engine=runtime_engine,
        )
    response = runtime_engine.evaluate(sig_req, decision, market_regime=market_regime)
    await persist_sizing_audit(sig_req, runtime_engine)
    from app.services.news_risk_lock import apply_news_risk_lock

    response = await apply_news_risk_lock(response, sig_req.symbol)

    # â”€â”€ 8. Log + persist â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    _log_evaluation(sig_req, response)
    await persist_evaluation(sig_req, payload, raw, response)
    try:
        from app.services.position_management import record_position_management

        await record_position_management(sig_req, raw, response)
    except Exception:
        logger.exception(
            "Position management persistence failed symbol=%s", sig_req.symbol
        )

    return EvaluationResult(
        response=response,
        mode=sig_req.mode,
        decision_created_utc=decision_created_utc,
        evaluation_purpose=evaluation_purpose,
        research_score=(
            _safe_float(raw.get("research_score"))
            if "research_score" in raw
            else None
        ),
        raw_action=decision.action,
    )


async def _build_position_context(req: SignalRequest) -> dict[str, Any] | None:
    """AÃ§Ä±k bot pozisyonu iÃ§in maliyet + K/Z baÄŸlamÄ± Ã¼ret; yoksa None.

    Maliyet ``bot_positions.avg_price``ten okunur (position_sync gÃ¼ncel
    tutar). DB hatasÄ± veya kayÄ±t yokluÄŸu evaluation'Ä± asla dÃ¼ÅŸÃ¼rmez.
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

    gateway_context = dict(req.gateway_position_context or {})
    bot_avg_cost = await _bot_average_cost_from_fill_ledger(req.symbol)
    cost_source = "BOT_FILL_LEDGER" if bot_avg_cost is not None else "UNAVAILABLE"
    if bot_avg_cost is None and row is not None and row.avg_price:
        bot_avg_cost = Decimal(str(row.avg_price))
        cost_source = "BOT_POSITION_CACHE"

    account_avg_raw = gateway_context.get("accountAvgCost")
    account_avg_cost = (
        Decimal(str(account_avg_raw)) if account_avg_raw not in (None, 0, "0") else None
    )
    account_net_qty = int(gateway_context.get("accountQtyNet") or req.total_account_qty)
    if (
        bot_avg_cost is None
        and account_avg_cost is not None
        and req.bot_position_qty > 0
        and req.bot_position_qty == account_net_qty
    ):
        bot_avg_cost = account_avg_cost
        cost_source = "MATRIX_ACCOUNT_AVG_COST_FULL_OWNERSHIP_FALLBACK"

    current_price = Decimal(str(gateway_context.get("currentPrice") or req.last_price))
    context: dict[str, Any] = {
        **gateway_context,
        "botQty": req.bot_position_qty,
        "accountQtyNet": account_net_qty,
        "accountQtyAvailable": req.account_available_qty,
        "accountAvgCost": float(account_avg_cost) if account_avg_cost else None,
        "botAvgCost": float(bot_avg_cost) if bot_avg_cost else None,
        "currentPrice": float(current_price),
        "botPositionValueTl": float(Decimal(req.bot_position_qty) * current_price),
        "costSource": cost_source,
    }
    if bot_avg_cost is not None and bot_avg_cost > 0 and current_price > 0:
        context["botUnrealizedPnlTl"] = float(
            (current_price - bot_avg_cost) * Decimal(req.bot_position_qty)
        )
        context["botUnrealizedPnlPct"] = float(
            (current_price - bot_avg_cost) / bot_avg_cost * Decimal("100")
        )
    return context


async def _bot_average_cost_from_fill_ledger(symbol: str) -> Decimal | None:
    """Compute bot-only average cost from monotonic, request-id unique fills."""
    try:
        async with async_session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(OrderLog)
                        .where(
                            OrderLog.symbol == symbol,
                            OrderLog.filled_qty > 0,
                        )
                        .order_by(OrderLog.created_at, OrderLog.id)
                    )
                )
                .scalars()
                .all()
            )
    except Exception:
        logger.exception("Bot fill ledger cost load failed symbol=%s", symbol)
        return None

    qty = Decimal("0")
    cost = Decimal("0")
    for order in rows:
        filled = Decimal(str(order.filled_qty or 0))
        if filled <= 0:
            continue
        if str(order.action).upper() == "BUY":
            price_raw = order.avg_price or order.rounded_limit_price or order.limit_price
            if not price_raw:
                continue
            price = Decimal(str(price_raw))
            qty += filled
            cost += filled * price
        elif str(order.action).upper() == "SELL" and qty > 0:
            released = min(qty, filled)
            average = cost / qty
            qty -= released
            cost -= released * average
    return cost / qty if qty > 0 and cost > 0 else None


def _log_evaluation(req: SignalRequest, response: SignalResponse) -> None:
    log_signal_evaluation(
        request_id=req.request_id,
        symbol=req.symbol,
        mode=req.mode.value,
        request=req.model_dump(by_alias=True, exclude={"mode"}, mode="json"),
        response=response.model_dump(by_alias=True, mode="json"),
    )




