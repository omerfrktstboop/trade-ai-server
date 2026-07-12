from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class KapEvent(Base):
    __tablename__ = "kap_events"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "title", "published_at", name="uq_kap_event_identity"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UNKNOWN"
    )
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, default="LOW")
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source: Mapped[str | None] = mapped_column(String(128))
    raw_json: Mapped[dict | None] = mapped_column(JSON)
    cached_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
