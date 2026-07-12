from __future__ import annotations
from datetime import datetime
from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class PositionManagementDecision(Base):
    __tablename__ = "position_management_decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    bot_qty: Mapped[float] = mapped_column(Float)
    avg_cost: Mapped[float | None] = mapped_column(Float)
    last_price: Mapped[float] = mapped_column(Float)
    unrealized_pnl_pct: Mapped[float | None] = mapped_column(Float)
    action: Mapped[str] = mapped_column(String(24), default="HOLD")
    suggested_sell_qty: Mapped[float] = mapped_column(Float, default=0)
    suggested_limit_price: Mapped[float | None] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    trailing_stop: Mapped[float | None] = mapped_column(Float)
    take_profit: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="SUGGESTED")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
