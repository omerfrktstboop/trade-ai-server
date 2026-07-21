"""Durable, fill-confirmed portfolio rotation state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RotationPlan(Base):
    __tablename__ = "rotation_plans"
    __table_args__ = (
        Index("ix_rotation_plans_account_state", "account_ref", "state"),
        UniqueConstraint("sell_request_id", name="uq_rotation_plans_sell_request_id"),
        UniqueConstraint("buy_request_id", name="uq_rotation_plans_buy_request_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_ref: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    target_symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    source_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    target_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)

    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_opportunity_score: Mapped[float] = mapped_column(Float, nullable=False)
    target_opportunity_score: Mapped[float] = mapped_column(Float, nullable=False)
    source_expected_return_pct: Mapped[float] = mapped_column(Float, nullable=False)
    target_expected_return_pct: Mapped[float] = mapped_column(Float, nullable=False)
    source_assessment_request_id: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    target_assessment_request_id: Mapped[str] = mapped_column(
        String(64), nullable=False
    )

    source_position_generation: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    source_fill_position_generation: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    sell_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    buy_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
