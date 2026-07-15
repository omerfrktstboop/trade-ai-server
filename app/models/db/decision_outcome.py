"""Decision outcome — forward-return / MFE / MAE measurement for every AI
evaluation (BUY, SELL, WAIT, blocked research candidates), not only executed
orders (Task 3). One row per request_id, created PENDING at evaluation-persist
time and later filled in by the outcome labeler using only real gateway
prices - never fabricated or backfilled with zero.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DecisionOutcome(Base):
    __tablename__ = "decision_outcomes"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_decision_outcomes_request_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    evaluation_purpose: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Legacy alias for final_action - kept for backward compatibility;
    # final_action is the field new code should read (Task 5).
    decision_action: Mapped[str] = mapped_column(String(8), nullable=False)

    # What the AI/system-gate/cache actually produced, before RiskEngine
    # gating - None only when no decision object was ever formed (e.g. the
    # kill switch's synthetic response). Distinct from final_action, which
    # may differ (an AI BUY the RiskEngine blocked to a final WAIT).
    raw_ai_action: Mapped[str | None] = mapped_column(String(8), nullable=True)
    final_action: Mapped[str | None] = mapped_column(String(8), nullable=True)
    allow_order: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    block_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # llm | cache | preflight-gate | system-gate (see payload["decisionSource"])
    decision_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    raw_ai_confidence: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    final_confidence: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    raw_ai_risk_score: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    final_risk_score: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)

    decision_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    decision_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    strategy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_context_schema_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    profile_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ai_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_model: Mapped[str | None] = mapped_column(String(64), nullable=True)

    discovery_sources: Mapped[list | None] = mapped_column(JSON, nullable=True)
    market_regime: Mapped[str | None] = mapped_column(String(32), nullable=True)
    trend_pre_score: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    research_score: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    confidence_score: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    risk_score: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)

    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)

    future_return_5m: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    future_return_15m: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 10), nullable=True
    )
    future_return_30m: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 10), nullable=True
    )
    future_return_60m: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 10), nullable=True
    )
    future_return_eod: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 10), nullable=True
    )
    mfe_pct: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    mae_pct: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)

    target_hit_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stop_hit_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    target_hit_before_stop: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    outcome_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING", index=True
    )
    unavailable_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
