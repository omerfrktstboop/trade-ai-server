"""Signal overrides — admin-injected one-shot BUY/SELL decisions for testing.

Bypasses the AI provider for a single symbol's next evaluation while still
going through every RiskEngine safety gate (cutoff time, daily limit,
symbol allow-list, mode-based allowOrder, SELL qty clamp, etc.).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SignalOverride(Base):
    __tablename__ = "signal_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(16), unique=True, index=True, nullable=False
    )

    action: Mapped[str] = mapped_column(String(8), nullable=False)  # BUY / SELL
    confidence: Mapped[float] = mapped_column(Float, default=100.0)
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    entry_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    reason: Mapped[str] = mapped_column(String(255), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
