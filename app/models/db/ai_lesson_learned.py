"""AI lessons learned — self-reflection output from the weekly review agent.

Each row is one lesson the system drew from a cluster of stop-loss trades
that closed at a loss during a review period. Never auto-applied to
``app/core/prompts.py`` — ``status`` stays ``PENDING_REVIEW`` until a human
consciously promotes (``APPLIED``) or rejects (``DISMISSED``) it. Changing
the live trading system prompt is a code change; the review agent's job
ends at proposing one, not making one.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Root-cause taxonomy the review LLM is asked to pick from (see
# app.core.prompts.get_review_system_prompt). Kept here (not a DB enum) so a
# new category doesn't require a migration — validated softly in
# review_agent instead.
ROOT_CAUSES: tuple[str, ...] = (
    "NEWS_MISREAD",
    "SMART_MONEY_MISREAD",
    "STOP_TOO_TIGHT",
    "TECHNICAL_MISREAD",
    "RISK_SIZING",
    "OTHER",
)

STATUS_PENDING = "PENDING_REVIEW"
STATUS_APPLIED = "APPLIED"
STATUS_DISMISSED = "DISMISSED"


class AiLessonLearned(Base):
    __tablename__ = "ai_lessons_learned"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    symbols_involved: Mapped[str] = mapped_column(String(256), default="")
    trades_reviewed_count: Mapped[int] = mapped_column(Integer, default=0)
    total_realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)

    root_cause: Mapped[str] = mapped_column(String(32), default="OTHER")
    lesson: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_rule: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Denetim izi: bu dersin türetildiği ham LLM cevabı (parse başarısız
    # olsa bile — o durumda lesson/proposed_rule alanları güvenli bir
    # varsayılana düşer, ham metin burada kaybolmaz).
    raw_llm_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default=STATUS_PENDING)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
