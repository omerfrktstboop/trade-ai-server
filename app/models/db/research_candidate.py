"""Persistent discovery candidates and their research timeline."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ResearchCandidate(Base):
    __tablename__ = "research_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(16), unique=True, index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="DETECTED", index=True
    )
    source: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    trend_pre_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    change_pct_daily: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_pct_30m: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_pct_60m: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_tl: Mapped[float | None] = mapped_column(Float, nullable=True)
    relative_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    technical_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    ai_action: Mapped[str | None] = mapped_column(String(10), nullable=True)
    ai_research_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_target_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    first_detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_successful_evaluation_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consecutive_pass_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    promoted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rejected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ResearchCandidateEvent(Base):
    __tablename__ = "research_candidate_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("research_candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
