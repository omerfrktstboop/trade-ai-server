"""Immutable audit trail for deterministic position sizing decisions."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Integer, JSON, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PositionSizingAudit(Base):
    __tablename__ = "position_sizing_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    trade_profile_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trade_profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    system_config_version: Mapped[str] = mapped_column(String(128), nullable=False)
    environment_config_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False
    )

    account_equity_tl: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    effective_available_cash_tl: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    risk_per_trade_pct: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    risk_budget_tl: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    raw_stop_distance_tl: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    slippage_buffer_tl: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    effective_stop_distance_tl: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    qty_by_risk: Mapped[int | None] = mapped_column(Integer)
    qty_by_cash: Mapped[int | None] = mapped_column(Integer)
    qty_by_account_exposure: Mapped[int | None] = mapped_column(Integer)
    qty_by_symbol_position: Mapped[int | None] = mapped_column(Integer)
    qty_by_order_value: Mapped[int | None] = mapped_column(Integer)
    qty_by_profile_max: Mapped[int | None] = mapped_column(Integer)
    final_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    order_value_tl: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    estimated_loss_at_stop_tl: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    binding_limits: Mapped[list[str]] = mapped_column(JSON, default=list)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    effective_risk_config: Mapped[dict] = mapped_column(JSON, nullable=False)
    calculation_details: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
