"""Signal domain models — request & response schemas."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Enums ──────────────────────────────────────────────────────────────────


class SignalAction(str, Enum):
    """Trading action derived from signal evaluation."""

    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"


class AgentAction(str, Enum):
    """Extended action for agentic multi-turn evaluation."""

    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"
    FETCH_DATA = "FETCH_DATA"


class DataRequestType(str, Enum):
    """Types of additional data the agent can request from the client."""

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


class SignalMode(str, Enum):
    """Signal processing mode — how aggressively orders are handled."""

    PAPER = "PAPER"
    MANUAL = "MANUAL"
    LIVE = "LIVE"


# ── Nested models ──────────────────────────────────────────────────────────


class EntryRange(BaseModel):
    """Price range for limit order entry."""

    min: float = Field(..., description="Lower bound of entry range")
    max: float = Field(..., description="Upper bound of entry range")


# ── Request ────────────────────────────────────────────────────────────────


class SignalRequest(BaseModel):
    """Incoming signal from the trading bot / scanner."""

    request_id: str = Field(..., alias="requestId")
    symbol: str
    timeframe: str

    # OHLCV
    last_price: float = Field(..., alias="lastPrice")
    open: float
    high: float
    low: float
    volume: float

    # Technical indicators (optional — some timeframes / providers omit these)
    rsi: Optional[float] = None
    ema20: Optional[float] = None
    ema50: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = Field(None, alias="macdSignal")

    # Position context
    bot_position_qty: float = Field(0.0, alias="botPositionQty")
    total_account_qty: float = Field(0.0, alias="totalAccountQty")
    locked_long_term_qty: float = Field(0.0, alias="lockedLongTermQty")

    # Daily trade count (fed by caller — e.g. Matriks IQ or DB)
    daily_trade_count: int = Field(0, alias="dailyTradeCount", ge=0)

    # Agentic session (used only by /signal/evaluate-agent)
    session_id: str = Field("", alias="sessionId")

    # Mode
    mode: SignalMode = SignalMode.PAPER

    model_config = ConfigDict(extra="allow", populate_by_name=True)


# ── Response ───────────────────────────────────────────────────────────────


class FetchData(BaseModel):
    """Data request sent back to the client when more context is needed."""

    target_symbol: str = Field(..., alias="targetSymbol")
    data_type: DataRequestType = Field(..., alias="dataType")
    reason: str


class SignalResponse(BaseModel):
    """AI evaluation result sent back to the trading bot."""

    request_id: str = Field(..., alias="requestId")
    symbol: str
    action: SignalAction
    qty: float
    order_type: OrderType = Field(..., alias="orderType")
    price: Optional[float] = None
    confidence_score: float = Field(..., alias="confidenceScore")
    risk_score: float = Field(..., alias="riskScore")
    allow_order: bool = Field(..., alias="allowOrder")
    requires_confirmation: bool = Field(False, alias="requiresConfirmation")
    reason: str
    entry_range: Optional[EntryRange] = Field(None, alias="entryRange")
    stop_loss: Optional[float] = Field(None, alias="stopLoss")
    target_price: Optional[float] = Field(None, alias="targetPrice")

    model_config = {"populate_by_name": True}


class AgentSignalResponse(BaseModel):
    """Agentic evaluation result — may contain FETCH_DATA instead of order info.

    This is the response for the /api/signal/evaluate-agent endpoint.
    When the agent needs more data, ``action=FETCH_DATA`` and ``fetchData``
    is populated with the target symbol and data type.  When enough data
    is available, the fields are identical to ``SignalResponse``.
    """

    request_id: str = Field(..., alias="requestId")
    symbol: str
    session_id: str = Field(..., alias="sessionId")
    action: AgentAction
    fetch_data: Optional[FetchData] = Field(None, alias="fetchData")

    # Fields populated for BUY/SELL/WAIT (final decision)
    qty: float = 0.0
    order_type: OrderType = Field(OrderType.NONE, alias="orderType")
    price: Optional[float] = None
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
    context_history: list[ContextStep] = Field(default_factory=list, alias="contextHistory")
    mode: SignalMode = SignalMode.PAPER

    model_config = ConfigDict(populate_by_name=True)


class AgenticSignalResponse(BaseModel):
    """Response for the agentic multi-turn signal evaluation endpoint."""

    request_id: str = Field(..., alias="requestId")
    session_id: str = Field(..., alias="sessionId")
    action: AgenticAction
    allow_order: bool = Field(..., alias="allowOrder")
    requires_confirmation: bool = Field(..., alias="requiresConfirmation")
    reason: str
    target_symbol: Optional[str] = Field(None, alias="targetSymbol")
    required_data_type: Optional[AgenticDataType] = Field(None, alias="requiredDataType")
    confidence_score: float = Field(..., alias="confidenceScore")
    risk_score: float = Field(..., alias="riskScore")
    qty: float
    order_type: OrderType = Field(..., alias="orderType")
    price: Optional[float] = None
    entry_range: Optional[EntryRange] = Field(None, alias="entryRange")
    stop_loss: Optional[float] = Field(None, alias="stopLoss")
    target_price: Optional[float] = Field(None, alias="targetPrice")

    model_config = ConfigDict(populate_by_name=True)
