"""Trade profiles — named risk/behavior presets for RiskEngine + bot config."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Float, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TradeProfile(Base):
    __tablename__ = "trade_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    risk_level: Mapped[str] = mapped_column(String(16), default="MEDIUM")

    # Selectable/enabled (soft-delete) — NOT the same as "currently active".
    # The system-wide active profile is tracked separately via the
    # activeTradeProfileCode SystemConfig row (see app/services/trade_profile.py).
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Safe fallback target when the active profile can't be resolved. Exactly
    # one row should have this set — enforced in the service layer, not here.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # Built-in profiles (CONSERVATIVE/NORMAL/AGGRESSIVE/HIGH_RISK) can't be
    # deleted, only edited in place or cloned into a new custom profile.
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)

    # Informational only — no gate currently enforces this.
    allowed_modes: Mapped[str] = mapped_column(
        String(64), default="PAPER,MANUAL,DEMO_LIVE,REAL_LIVE"
    )

    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    risk_per_trade_pct: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal("0.50"), nullable=False
    )
    max_cash_utilization_pct: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal("25"), nullable=False
    )
    max_account_exposure_pct: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal("50"), nullable=False
    )
    max_order_value_tl: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    max_qty_per_order: Mapped[int] = mapped_column(Integer, nullable=False)
    max_position_value_per_symbol: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False
    )
    min_order_value_tl: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal("1"), nullable=False
    )
    min_stop_distance_pct: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal("0.10"), nullable=False
    )
    max_stop_distance_pct: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal("10"), nullable=False
    )
    minimum_stop_slippage_pct: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal("0.05"), nullable=False
    )
    maximum_stop_slippage_pct: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal("1"), nullable=False
    )
    profile_stop_slippage_pct: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal("0.20"), nullable=False
    )
    max_account_data_age_seconds: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal("60"), nullable=False
    )
    max_orders_per_day: Mapped[int] = mapped_column(Integer, nullable=False)
    max_orders_per_symbol_per_day: Mapped[int] = mapped_column(Integer, nullable=False)

    min_confidence_for_buy: Mapped[float] = mapped_column(Float, nullable=False)
    min_confidence_for_sell: Mapped[float] = mapped_column(Float, nullable=False)
    max_natr_for_buy: Mapped[float] = mapped_column(Float, nullable=False)
    max_depth_queue_drop_pct_for_buy: Mapped[float] = mapped_column(
        Float, nullable=False
    )
    max_spread_pct_for_buy: Mapped[float] = mapped_column(
        Float, default=0.50, nullable=False
    )
    min_depth_bid_ask_ratio_top10_for_buy: Mapped[float] = mapped_column(
        Float, default=0.60, nullable=False
    )
    max_depth_sell_pressure_score_for_buy: Mapped[float] = mapped_column(
        Float, default=80.0, nullable=False
    )
    block_buy_on_strong_sell_pressure: Mapped[bool] = mapped_column(
        Boolean, default=True
    )
    block_buy_on_near_ask_wall: Mapped[bool] = mapped_column(Boolean, default=False)
    near_wall_distance_pct: Mapped[float] = mapped_column(
        Float, default=0.30, nullable=False
    )

    require_alpha_trend_alignment: Mapped[bool] = mapped_column(Boolean, default=True)
    require_indicator_consensus_alignment: Mapped[bool] = mapped_column(
        Boolean, default=True
    )
    allow_sell_long_term: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_short_selling: Mapped[bool] = mapped_column(Boolean, default=False)

    allow_real_live: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_demo_live: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_margin_buying: Mapped[bool] = mapped_column(Boolean, default=False)

    scan_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    max_fetch_loop_per_session: Mapped[int] = mapped_column(Integer, default=3)
    order_time_in_force: Mapped[str] = mapped_column(String(16), default="Day")
    indicator_period: Mapped[str] = mapped_column(String(16), default="Min5")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
