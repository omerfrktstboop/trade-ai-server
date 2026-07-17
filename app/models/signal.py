"""Signal domain models — request & response schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from app.services.position_sizing import AccountSizingContext


# ── Enums ──────────────────────────────────────────────────────────────────


class SignalAction(str, Enum):
    """Trading action derived from signal evaluation."""

    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"


class AgentAction(str, Enum):
    """.. deprecated:: 1.0
       Use :class:`AgenticAction` instead — canonical for v2 agentic protocol.
       Retained for backward compatibility with :class:`AgentSignalResponse`.

    Extended action for agentic multi-turn evaluation."""

    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"
    FETCH_DATA = "FETCH_DATA"


class DataRequestType(str, Enum):
    """.. deprecated:: 1.0
       Use :class:`AgenticDataType` instead - canonical for v2 agentic protocol.

    Types of additional data the agent can request from the client."""

    INTRADAY_OHLC = "INTRADAY_OHLC"
    VOLUME_DISTRIBUTION = "VOLUME_DISTRIBUTION"
    ORDER_FLOW = "ORDER_FLOW"
    NEWS_DETAIL = "NEWS_DETAIL"
    FUND_FLOW = "FUND_FLOW"


# ── New agentic models (v2 — user-specified names) ────────────────────


class AgenticDataType(str, Enum):
    """Data types the agent can request from Matriks IQ client."""

    DEPTH = "DEPTH"
    AKD = "AKD"
    OHLCV = "OHLCV"
    TECHNICAL = "TECHNICAL"
    NEWS = "NEWS"
    FUND = "FUND"
    BROKER_FLOW = "BROKER_FLOW"


class AgenticAction(str, Enum):
    """Actions for the agentic multi-turn evaluation."""

    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"
    FETCH_DATA = "FETCH_DATA"


class OrderType(str, Enum):
    """Order execution type."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    NONE = "NONE"


# v2: SignalMode (PAPER/MANUAL/LIVE/DEMO_LIVE/REAL_LIVE) kaldırıldı. Çalışma
# modu artık yalnızca admin config'deki systemMode (OBSERVE_ONLY/AUTO_TRADE);
# DEMO/REAL sadece gateway'in bildirdiği accountType'tır, çalışma modu değil.


# ── Nested models ──────────────────────────────────────────────────────────


class EntryRange(BaseModel):
    """Price range for limit order entry."""

    min: Decimal = Field(..., description="Lower bound of entry range")
    max: Decimal = Field(..., description="Upper bound of entry range")


# ── Request ────────────────────────────────────────────────────────────────


class SignalRequest(BaseModel):
    """Incoming signal from the trading bot / scanner."""

    request_id: str = Field(..., alias="requestId")
    symbol: str
    timeframe: str
    requested_timeframe: Optional[str] = Field(None, alias="requestedTimeframe")
    actual_bar_period: Optional[str] = Field(None, alias="actualBarPeriod")
    actual_bar_period_seconds: Optional[int] = Field(
        None, alias="actualBarPeriodSeconds", ge=1
    )
    bar_period_source: Optional[str] = Field(None, alias="barPeriodSource")
    timeframe_mismatch: bool = Field(False, alias="timeframeMismatch")
    indicator_period: Optional[str] = Field(None, alias="indicatorPeriod")
    indicator_period_seconds: Optional[int] = Field(
        None, alias="indicatorPeriodSeconds", ge=1
    )
    instrument_type: Optional[str] = Field(None, alias="instrumentType")

    # OHLCV
    last_price: float = Field(..., alias="lastPrice")
    open: float
    high: float
    low: float
    volume: float
    bar_volume: Optional[float] = Field(None, alias="barVolume")
    bar_volume_source: Optional[str] = Field(None, alias="barVolumeSource")
    bar_volume_unit: Optional[str] = Field(None, alias="barVolumeUnit")
    bar_volume_reliable: Optional[bool] = Field(None, alias="barVolumeReliable")
    session_turnover_tl: Optional[float] = Field(None, alias="sessionTurnoverTl")
    total_vol: Optional[float] = Field(None, alias="totalVol")
    total_vol_source: Optional[str] = Field(None, alias="totalVolSource")
    total_vol_unit: Optional[str] = Field(None, alias="totalVolUnit")
    total_vol_reliable: Optional[bool] = Field(None, alias="totalVolReliable")
    volume_indicator_value: Optional[float] = Field(None, alias="volumeIndicatorValue")
    volume_tl_indicator_value: Optional[float] = Field(
        None, alias="volumeTlIndicatorValue"
    )
    bar_closed: Optional[bool] = Field(None, alias="barClosed")
    bar_is_new: Optional[bool] = Field(None, alias="barIsNew")
    bar_data_index: Optional[int] = Field(None, alias="barDataIndex")
    # Data-quality flag: Matriks intraday snapshots often lack real bar data,
    # so open/high/low may just be lastPrice repeated (see BuildMarketData in
    # TradeAiAgenticBot.cs). When false, the AI must not treat the flat range
    # as evidence of real price action.
    ohlc_reliable: Optional[bool] = Field(None, alias="ohlcReliable")
    ohlc_source: Optional[str] = Field(None, alias="ohlcSource")

    # Data-quality flag for lastPrice itself: when the live quote is zero
    # (feed hiccup), the bot falls back to the last known valid quote (up to
    # 8h old — see ReadMarketQuote in TradeAiAgenticBot.cs) rather than
    # sending a raw zero. When false, lastPrice is not a fresh live tick.
    quote_reliable: Optional[bool] = Field(None, alias="quoteReliable")
    price_source: Optional[str] = Field(None, alias="priceSource")

    # Technical indicators (optional — some timeframes / providers omit these)
    rsi: Optional[float] = None
    ema20: Optional[float] = None
    ema50: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = Field(None, alias="macdSignal")

    # Optional Matriks-derived technical feature layer. These fields are
    # signal inputs only; RiskEngine treats them as guards when present.
    alpha_trend_signal: Optional[str] = Field(None, alias="alphaTrendSignal")
    alpha_trend_mode: Optional[str] = Field(None, alias="alphaTrendMode")
    indicator_buy_count: Optional[int] = Field(None, alias="indicatorBuyCount", ge=0)
    indicator_sell_count: Optional[int] = Field(None, alias="indicatorSellCount", ge=0)
    indicator_neutral_count: Optional[int] = Field(
        None, alias="indicatorNeutralCount", ge=0
    )
    indicator_consensus: Optional[str] = Field(None, alias="indicatorConsensus")
    indicator_consensus_ratio: Optional[float] = Field(
        None, alias="indicatorConsensusRatio", ge=0, le=1
    )
    atr: Optional[float] = None
    natr: Optional[float] = None
    atr_period: Optional[int] = Field(None, alias="atrPeriod")
    atr_timeframe: Optional[str] = Field(None, alias="atrTimeframe")
    volatility_metric_source: Optional[str] = Field(
        None, alias="volatilityMetricSource"
    )
    close_change_volatility_proxy: Optional[float] = Field(
        None, alias="closeChangeVolatilityProxy"
    )
    adx: Optional[float] = None
    # MOST (hareketli stop) — gateway native göstergeden üretir (v2 Faz 3).
    most: Optional[float] = None
    most_signal: Optional[str] = Field(None, alias="mostSignal")
    obv_slope: Optional[float] = Field(None, alias="obvSlope")
    vwap_distance_pct: Optional[float] = Field(None, alias="vwapDistancePct")
    depth_bid1_size: Optional[float] = Field(None, alias="depthBid1Size")
    depth_bid1_max_size: Optional[float] = Field(None, alias="depthBid1MaxSize")
    depth_queue_drop_pct: Optional[float] = Field(None, alias="depthQueueDropPct")
    depth_reliable: Optional[bool] = Field(None, alias="depthReliable")
    depth_levels_used: Optional[int] = Field(None, alias="depthLevelsUsed")
    spread_pct: Optional[float] = Field(None, alias="spreadPct")
    depth_bid_ask_ratio_top5: Optional[float] = Field(
        None, alias="depthBidAskRatioTop5"
    )
    depth_bid_ask_ratio_top10: Optional[float] = Field(
        None, alias="depthBidAskRatioTop10"
    )
    depth_bid_ask_ratio_top25: Optional[float] = Field(
        None, alias="depthBidAskRatioTop25"
    )
    depth_imbalance_top5: Optional[float] = Field(None, alias="depthImbalanceTop5")
    depth_imbalance_top10: Optional[float] = Field(None, alias="depthImbalanceTop10")
    depth_imbalance_top25: Optional[float] = Field(None, alias="depthImbalanceTop25")
    depth_bid_concentration_top3_pct: Optional[float] = Field(
        None, alias="depthBidConcentrationTop3Pct"
    )
    depth_ask_concentration_top3_pct: Optional[float] = Field(
        None, alias="depthAskConcentrationTop3Pct"
    )
    depth_largest_bid_wall_distance_pct: Optional[float] = Field(
        None, alias="depthLargestBidWallDistancePct"
    )
    depth_largest_ask_wall_distance_pct: Optional[float] = Field(
        None, alias="depthLargestAskWallDistancePct"
    )
    depth_nearest_bid_wall_distance_pct: Optional[float] = Field(
        None, alias="depthNearestBidWallDistancePct"
    )
    depth_nearest_ask_wall_distance_pct: Optional[float] = Field(
        None, alias="depthNearestAskWallDistancePct"
    )
    depth_buy_pressure_score: Optional[float] = Field(
        None, alias="depthBuyPressureScore"
    )
    depth_sell_pressure_score: Optional[float] = Field(
        None, alias="depthSellPressureScore"
    )
    depth_order_book_signal: Optional[str] = Field(None, alias="depthOrderBookSignal")
    depth_summary: Optional[str] = Field(None, alias="depthSummary")
    depth_wall_concentration_risk: Optional[bool] = Field(
        None, alias="depthWallConcentrationRisk"
    )
    quote_age_seconds: Optional[float] = Field(None, alias="quoteAgeSeconds")
    ohlcv_age_seconds: Optional[float] = Field(None, alias="ohlcvAgeSeconds")
    depth_age_seconds: Optional[float] = Field(None, alias="depthAgeSeconds")
    quote_event_utc: Optional[datetime] = Field(None, alias="quoteEventUtc")
    depth_event_utc: Optional[datetime] = Field(None, alias="depthEventUtc")
    bar_event_utc: Optional[datetime] = Field(None, alias="barEventUtc")
    last_trade_utc: Optional[datetime] = Field(None, alias="lastTradeUtc")
    quote_read_utc: Optional[datetime] = Field(None, alias="quoteReadUtc")
    depth_read_utc: Optional[datetime] = Field(None, alias="depthReadUtc")
    quote_timestamp_source: Optional[str] = Field(None, alias="quoteTimestampSource")
    depth_timestamp_source: Optional[str] = Field(None, alias="depthTimestampSource")
    depth_event_timestamp_available: Optional[bool] = Field(
        None, alias="depthEventTimestampAvailable"
    )
    depth_read_latency_seconds: Optional[float] = Field(
        None, alias="depthReadLatencySeconds"
    )
    bar_timestamp_source: Optional[str] = Field(None, alias="barTimestampSource")
    bar_time_reliable: Optional[bool] = Field(None, alias="barTimeReliable")
    bar_timestamp_fallback_observation_utc: Optional[datetime] = Field(
        None, alias="barTimestampFallbackObservationUtc"
    )
    bar_timestamp_fallback_observation_source: Optional[str] = Field(
        None, alias="barTimestampFallbackObservationSource"
    )
    snapshot_built_utc: Optional[datetime] = Field(None, alias="snapshotBuiltUtc")
    symbol_trend_regime: Optional[str] = Field(None, alias="symbolTrendRegime")
    # Deprecated v1 alias; when supplied it has only symbol-trend semantics.
    market_regime: Optional[str] = Field(None, alias="marketRegime")
    macro_market_regime: Optional[str] = Field(None, alias="macroMarketRegime")
    macro_market_regime_symbol: Optional[str] = Field(
        None, alias="macroMarketRegimeSymbol"
    )

    # Position context
    bot_position_qty: int = Field(0, alias="botPositionQty")
    total_account_qty: int = Field(0, alias="totalAccountQty")
    account_available_qty: int = Field(0, alias="accountAvailableQty")
    locked_long_term_qty: int = Field(0, alias="lockedLongTermQty")
    gateway_position_context: dict[str, Any] | None = Field(
        None, alias="positionContext"
    )
    account_sizing_context: AccountSizingContext | None = Field(
        None, alias="accountSizingContext"
    )
    trade_eligible: bool = Field(False, alias="tradeEligible")
    evaluation_purpose: str = Field("TRADING", alias="evaluationPurpose")

    # Daily trade count (fed by caller — e.g. Matriks IQ or DB)
    daily_trade_count: int = Field(0, alias="dailyTradeCount", ge=0)
    daily_accepted_order_count: int = Field(0, alias="dailyAcceptedOrderCount", ge=0)
    daily_filled_order_count: int = Field(0, alias="dailyFilledOrderCount", ge=0)

    # Legacy field from the removed agentic protocol; retained so old
    # clients posting sessionId to /signal/evaluate still validate.
    session_id: str = Field("", alias="sessionId")

    # v2: mode alanı kaldırıldı (çalışma modu artık global systemMode).

    model_config = ConfigDict(extra="allow", populate_by_name=True)


# ── Response ───────────────────────────────────────────────────────────────


class FetchData(BaseModel):
    """Data request sent back to the client when more context is needed."""

    target_symbol: str = Field(..., alias="targetSymbol")
    data_type: AgenticDataType = Field(..., alias="dataType")
    reason: str


class SignalResponse(BaseModel):
    """AI evaluation result sent back to the trading bot."""

    request_id: str = Field(..., alias="requestId")
    symbol: str
    action: SignalAction
    qty: int
    order_type: OrderType = Field(..., alias="orderType")
    price: Optional[Decimal] = None
    confidence_score: float = Field(..., alias="confidenceScore")
    risk_score: float = Field(..., alias="riskScore")
    allow_order: bool = Field(..., alias="allowOrder")
    requires_confirmation: bool = Field(False, alias="requiresConfirmation")
    reason: str
    entry_range: Optional[EntryRange] = Field(None, alias="entryRange")
    stop_loss: Optional[Decimal] = Field(None, alias="stopLoss")
    target_price: Optional[Decimal] = Field(None, alias="targetPrice")

    model_config = {"populate_by_name": True}


class AgentSignalResponse(BaseModel):
    """.. deprecated:: 1.0
       Use :class:`AgenticSignalResponse` instead — canonical for v2 protocol.
       Retained only for backward compatibility; not used by current endpoints.

    Agentic evaluation result — may contain FETCH_DATA in...[truncated]
    is populated with the target symbol and data type.  When enough data
    is available, the fields are identical to ``SignalResponse``.
    """

    request_id: str = Field(..., alias="requestId")
    symbol: str
    session_id: str = Field(..., alias="sessionId")
    action: AgentAction
    fetch_data: Optional[FetchData] = Field(None, alias="fetchData")

    # Fields populated for BUY/SELL/WAIT (final decision)
    qty: FiniteFloat = 0.0
    order_type: OrderType = Field(OrderType.NONE, alias="orderType")
    price: Optional[FiniteFloat] = None
    confidence_score: float = Field(0.0, alias="confidenceScore")
    risk_score: float = Field(0.0, alias="riskScore")
    allow_order: bool = Field(False, alias="allowOrder")
    requires_confirmation: bool = Field(False, alias="requiresConfirmation")
    reason: str = ""
    entry_range: Optional[EntryRange] = Field(None, alias="entryRange")
    stop_loss: Optional[float] = Field(None, alias="stopLoss")
    target_price: Optional[float] = Field(None, alias="targetPrice")

    model_config = {"populate_by_name": True}


# ── v2 Agentic models (user-specified names) ──────────────────────────────


class MarketDataPayload(BaseModel):
    """Single market data payload from Matriks IQ client."""

    symbol: str
    data_type: AgenticDataType = Field(..., alias="dataType")
    payload: dict
    timestamp: Optional[datetime] = None


class ContextStep(BaseModel):
    """A single step in the agentic multi-turn context history."""

    step_no: int = Field(..., alias="stepNo")
    symbol: str
    data_type: AgenticDataType = Field(..., alias="dataType")
    payload: dict
    reason: Optional[str] = None


class AgenticSignalRequest(BaseModel):
    """Request for the agentic multi-turn signal evaluation endpoint."""

    request_id: str = Field(..., alias="requestId")
    session_id: Optional[str] = Field(None, alias="sessionId")
    symbol: str
    market_data: MarketDataPayload = Field(..., alias="marketData")
    context_history: list[ContextStep] = Field(
        default_factory=list, alias="contextHistory"
    )

    model_config = ConfigDict(populate_by_name=True)


class AgenticSignalResponse(BaseModel):
    """Response for the agentic multi-turn signal evaluation endpoint."""

    request_id: str = Field(..., alias="requestId")
    session_id: str = Field(..., alias="sessionId")
    symbol: str
    action: AgenticAction
    allow_order: bool = Field(..., alias="allowOrder")
    requires_confirmation: bool = Field(..., alias="requiresConfirmation")
    reason: str
    target_symbol: Optional[str] = Field(None, alias="targetSymbol")
    required_data_type: Optional[AgenticDataType] = Field(
        None, alias="requiredDataType"
    )
    confidence_score: float = Field(..., alias="confidenceScore")
    risk_score: float = Field(..., alias="riskScore")
    qty: FiniteFloat
    order_type: OrderType = Field(..., alias="orderType")
    price: Optional[FiniteFloat] = None
    entry_range: Optional[EntryRange] = Field(None, alias="entryRange")
    stop_loss: Optional[float] = Field(None, alias="stopLoss")
    target_price: Optional[float] = Field(None, alias="targetPrice")
    config_version: str = Field("", alias="configVersion")
    config_hash: str = Field("", alias="configHash")

    model_config = ConfigDict(populate_by_name=True)
