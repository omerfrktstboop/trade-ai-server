"""Market snapshot — OHLCV + indicators at evaluation time."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)

    # OHLCV
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)

    # Indicators
    rsi: Mapped[float | None] = mapped_column(Float, nullable=True)
    ema20: Mapped[float | None] = mapped_column(Float, nullable=True)
    ema50: Mapped[float | None] = mapped_column(Float, nullable=True)
    macd: Mapped[float | None] = mapped_column(Float, nullable=True)
    macd_signal: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_ask_ratio_top5: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_ask_ratio_top10: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_ask_ratio_top25: Mapped[float | None] = mapped_column(Float, nullable=True)
    imbalance_top10: Mapped[float | None] = mapped_column(Float, nullable=True)
    imbalance_top25: Mapped[float | None] = mapped_column(Float, nullable=True)
    largest_bid_wall_distance_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    largest_ask_wall_distance_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    depth_buy_pressure_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    depth_sell_pressure_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    depth_order_book_signal: Mapped[str | None] = mapped_column(String(32), nullable=True)
    depth_reliable: Mapped[bool | None] = mapped_column(nullable=True)

    position_qty: Mapped[float] = mapped_column(Float, default=0.0)
    total_account_qty: Mapped[float] = mapped_column(Float, default=0.0)
    locked_long_term_qty: Mapped[float] = mapped_column(Float, default=0.0)

    mode: Mapped[str] = mapped_column(String(16), default="PAPER")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
