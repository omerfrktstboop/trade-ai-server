"""Risk decision — final safety-checked output from the risk engine."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RiskDecision(Base):
    __tablename__ = "risk_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), index=True, nullable=False)

    action: Mapped[str] = mapped_column(String(8), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    allow_order: Mapped[bool] = mapped_column(Boolean, default=False)

    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    entry_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    order_type: Mapped[str] = mapped_column(String(16), default="NONE")
    qty: Mapped[float] = mapped_column(Float, default=0.0)

    # v2: mod kaldırıldı; kolon geçmiş/gösterim için kalır (default OBSERVE_ONLY).
    mode: Mapped[str] = mapped_column(String(16), default="OBSERVE_ONLY")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
