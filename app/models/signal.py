"""Signal domain models — request & response schemas."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────────


class SignalAction(str, Enum):
    """Trading action derived from signal evaluation."""

    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"


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

    # Mode
    mode: SignalMode = SignalMode.PAPER

    model_config = {"populate_by_name": True}


# ── Response ───────────────────────────────────────────────────────────────


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
