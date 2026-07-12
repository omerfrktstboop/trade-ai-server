"""Locked positions — long-term holds or locked lots that shouldn't be auto-sold."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LockedPosition(Base):
    __tablename__ = "locked_positions"
    __table_args__ = (
        CheckConstraint("qty >= 0", name="ck_locked_positions_qty_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True, nullable=False)

    qty: Mapped[float] = mapped_column(Float, default=0.0)
    lock_type: Mapped[str] = mapped_column(
        String(32), default="LONG_TERM", nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
