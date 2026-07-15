"""Market observation — one timestamped, quality-flagged price sample per
symbol, collected from the gateway snapshots the scanner and stop-loss
guard already fetch for their own purposes (Task 3). This is the real,
non-fabricated data source the outcome labeler (Task 4) reads horizon
prices and MFE/MAE from - never a single after-the-fact snapshot.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MarketObservation(Base):
    __tablename__ = "market_observations"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "observed_at",
            "bar_period",
            "price_source",
            name="uq_market_observations_symbol_time_period_source",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    # Real gateway event timestamp (quoteEventUtc/barEventUtc/snapshotBuiltUtc)
    # when available; SERVER_OBSERVED_AT when observed_at had to fall back to
    # server retrieval time (Task 3.2) - never blended silently.
    observed_at_source: Mapped[str] = mapped_column(String(24), nullable=False)

    last_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    high: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    low: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    close: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)

    # NOT NULL with explicit sentinels (UNKNOWN_PERIOD / UNKNOWN_SOURCE) so
    # the dedup unique key below actually works - a NULL never equals another
    # NULL in SQL, which would silently defeat the constraint (Fix 7).
    bar_period: Mapped[str] = mapped_column(
        String(16), nullable=False, default="UNKNOWN_PERIOD"
    )
    bar_closed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Real gateway bar boundaries when derivable (barEventUtc +
    # actualBarPeriodSeconds); None when the gateway did not report them. Used
    # by the outcome labeler to tell a bar that started *after* the decision
    # (usable as full OHLC) from the decision-crossing bar (Fix 4).
    bar_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    bar_end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    quote_reliable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ohlc_reliable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    quote_age_seconds: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    ohlcv_age_seconds: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    price_source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="UNKNOWN_SOURCE"
    )

    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
