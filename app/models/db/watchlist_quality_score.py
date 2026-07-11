from __future__ import annotations
from datetime import datetime
from sqlalchemy import DateTime, Float, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base
class WatchlistQualityScore(Base):
    __tablename__ = "watchlist_quality_scores"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    quality_score: Mapped[float] = mapped_column(Float)
    momentum_score: Mapped[float] = mapped_column(Float)
    volume_score: Mapped[float] = mapped_column(Float)
    depth_score: Mapped[float] = mapped_column(Float)
    news_score: Mapped[float] = mapped_column(Float, default=50)
    risk_score: Mapped[float] = mapped_column(Float, default=50)
    reason_json: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
