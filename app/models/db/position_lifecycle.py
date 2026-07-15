"""Position lifecycle — one row per symbol position from open (0->positive
qty) to full close (qty back to zero). Unlike BotPosition (all-time blended
average cost, gateway-truth cache for sizing/display), a lifecycle is scoped
to a single open->close episode so realized P&L and the active stop-loss can
be computed and bound correctly (Task 1.3, Task 4).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Index, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PositionLifecycle(Base):
    __tablename__ = "position_lifecycles"
    __table_args__ = (
        Index("ix_position_lifecycles_symbol_status", "symbol", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    entry_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    current_qty: Mapped[Decimal] = mapped_column(
        Numeric(28, 10), nullable=False, default=Decimal("0")
    )
    average_entry_price: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 10), nullable=True
    )

    gross_buy_value_tl: Mapped[Decimal] = mapped_column(
        Numeric(28, 10), nullable=False, default=Decimal("0")
    )
    gross_sell_value_tl: Mapped[Decimal] = mapped_column(
        Numeric(28, 10), nullable=False, default=Decimal("0")
    )
    total_buy_cost_tl: Mapped[Decimal] = mapped_column(
        Numeric(28, 10), nullable=False, default=Decimal("0")
    )
    total_sell_cost_tl: Mapped[Decimal] = mapped_column(
        Numeric(28, 10), nullable=False, default=Decimal("0")
    )
    gross_realized_pnl_tl: Mapped[Decimal] = mapped_column(
        Numeric(28, 10), nullable=False, default=Decimal("0")
    )
    net_realized_pnl_tl: Mapped[Decimal] = mapped_column(
        Numeric(28, 10), nullable=False, default=Decimal("0")
    )

    initial_stop_loss: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 10), nullable=True
    )
    active_stop_loss: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 10), nullable=True
    )
    initial_target_price: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 10), nullable=True
    )
    active_target_price: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 10), nullable=True
    )

    strategy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_context_schema_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    config_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    profile_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_source: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # VERIFIED | PARTIAL | BACKFILL_UNAVAILABLE | RECONCILED | MANUAL_REVIEW
    # (Task 7). Can only ever get "worse" after opening - a lifecycle that
    # starts BACKFILL_UNAVAILABLE never becomes VERIFIED/RECONCILED, since
    # its buy-cost history is permanently unknown regardless of later real
    # fills applied to it.
    data_quality: Mapped[str] = mapped_column(
        String(24), nullable=False, default="VERIFIED"
    )
    is_backfilled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    backfill_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # True only when the lifecycle's full buy-cost basis is real fill data
    # (FILL_LEDGER or RECONCILIATION) - performance_report.py's strategy
    # metrics (profit factor, win rate, ...) only include pnl_verified=true
    # closed lifecycles by default (Task 8).
    pnl_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # FILL_LEDGER | LEGACY_POSITION_BACKFILL | RECONCILIATION
    measurement_source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="FILL_LEDGER"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
