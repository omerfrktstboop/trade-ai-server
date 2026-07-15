"""Context/payload builders for the evaluator: turn a SignalRequest (or a
raw Matriks snapshot) into the AI provider payload / AiDecisionContext, and
build a SignalRequest from a gateway snapshot in the first place.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.risk_config import RiskConfig, risk_config
from app.db.session import async_session_factory
from sqlalchemy import select

from app.models.ai_decision_context import AiDecisionContext
from app.models.db import BotPosition as BotPositionModel
from app.models.db import OrderLog
from app.models.signal import (
    SignalMode,
    SignalRequest,
)
from app.services.market_data_contract import normalize_snapshot_payload

logger = logging.getLogger(__name__)


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
    a placeholder and normally not passed - kept in the signature so wiring a
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
        summary = item.get("summary") or item.get("content")
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
            "topBuyer": (
                top_buyers[0].get("name")
                if top_buyers and isinstance(top_buyers[0], dict)
                else None
            ),
            "topSeller": (
                top_sellers[0].get("name")
                if top_sellers and isinstance(top_sellers[0], dict)
                else None
            ),
        }
    if isinstance(kap_entry, dict) and kap_entry:
        risk_events = kap_entry.get("riskEvents24h") or []
        events["kap"] = {
            "blockingRisk": kap_entry.get("hasBlockingRisk"),
            "activeRiskCount": len(risk_events)
            if isinstance(risk_events, list)
            else None,
            "unknownDateRisk": kap_entry.get("hasUnknownDateRisk"),
        }

    depth: dict[str, Any] | None = None
    if req.depth_reliable is not None:
        depth = {"reliable": req.depth_reliable}
        if req.depth_reliable is False:
            depth["signal"] = "UNAVAILABLE"
        else:
            depth.update(
                {
                    "spreadPct": req.spread_pct,
                    "buyPressure": min(
                        1.0, max(0.0, (req.depth_buy_pressure_score or 0.0) / 100.0)
                    ),
                    "signal": req.depth_order_book_signal,
                    "bidAskRatio": req.depth_bid_ask_ratio_top5,
                    "nearestBidWallDistancePct": req.depth_nearest_bid_wall_distance_pct,
                    "nearestAskWallDistancePct": req.depth_nearest_ask_wall_distance_pct,
                    "wallConcentrationRisk": req.depth_wall_concentration_risk,
                }
            )

    position: dict[str, Any] | None = None
    if req.bot_position_qty > 0:
        position = {
            "botQty": req.bot_position_qty,
            "botAvgCost": (position_context or {}).get("botAvgCost"),
            "unrealizedPnlPct": (position_context or {}).get("botUnrealizedPnlPct"),
            "lockedLongTerm": req.locked_long_term_qty > 0,
        }

    context = AiDecisionContext.model_validate(
        {
            "symbol": symbol,
            "period": {
                "requested": req.requested_timeframe or req.timeframe,
                "actual": req.actual_bar_period,
                "mismatch": req.timeframe_mismatch,
            },
            "profile": profile,
            "evaluationPurpose": req.evaluation_purpose,
            "dataQuality": {
                "quoteAgeSec": req.quote_age_seconds,
                "ohlcvAgeSec": req.ohlcv_age_seconds,
                "depthAgeSec": req.depth_age_seconds,
                "quoteReliable": req.quote_reliable,
                "ohlcReliable": req.ohlc_reliable,
            },
            "price": {
                "last": req.last_price,
                "open": req.open,
                "high": req.high,
                "low": req.low,
            },
            "market": {
                "barVolume": req.bar_volume,
                "sessionTurnoverTl": req.session_turnover_tl,
                "macroMarketRegime": macro_market_regime,
                "symbolTrendRegime": req.symbol_trend_regime or req.market_regime,
            },
            "technical": {
                "rsi": req.rsi,
                "ema20": req.ema20,
                "ema50": req.ema50,
                "macd": req.macd,
                "macdSignal": req.macd_signal,
                "atr": req.atr,
                "natr": req.natr,
                "adx": req.adx,
                "obvSlope": req.obv_slope,
                "vwapDistancePct": req.vwap_distance_pct,
                "alphaTrendSignal": req.alpha_trend_signal,
                "indicatorConsensus": req.indicator_consensus,
                "indicatorConsensusRatio": req.indicator_consensus_ratio,
                "indicatorBuyCount": req.indicator_buy_count,
                "indicatorSellCount": req.indicator_sell_count,
                "indicatorNeutralCount": req.indicator_neutral_count,
            },
            "depth": depth,
            "position": position,
            "events": events or None,
        }
    )
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
        closeChangeVolatilityProxy=_payload_get(payload, "closeChangeVolatilityProxy"),
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


def _build_request_id(symbol: str) -> str:
    """Eski bot'un BuildRequestId formatiyla uyumlu: SYMBOL-yyyyMMdd-HHmmss-scan."""
    return f"{symbol}-{datetime.now():%Y%m%d-%H%M%S}-scan"


def _snapshot_step(
    step_no: int, symbol: str, data_type: str, payload: dict[str, Any], reason: str
) -> dict[str, Any]:
    """AI payload'undaki ``agenticSteps`` girdisi - eski ContextStep semasi
    (stepNo/symbol/dataType/payload/reason) korunuyor ki prompt degismesin."""
    return {
        "stepNo": step_no,
        "symbol": symbol,
        "dataType": data_type,
        "payload": payload,
        "reason": reason,
    }


async def _build_position_context(req: SignalRequest) -> dict[str, Any] | None:
    """Acik bot pozisyonu icin maliyet + K/Z baglami uret; yoksa None.

    Maliyet ``bot_positions.avg_price``ten okunur (position_sync guncel
    tutar). DB hatasi veya kayit yoklugu evaluation'i asla dusurmez.
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
            price_raw = (
                order.avg_price or order.rounded_limit_price or order.limit_price
            )
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
