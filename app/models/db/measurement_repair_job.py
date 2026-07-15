"""Measurement repair job — queue of measurement-layer failures (fill ledger,
lifecycle, or outcome tracking) that need a retried, out-of-band repair pass
instead of being silently dropped when the inline callback-time attempt
fails (Task 1.2). Never touches the order-dispatch path; purely a
measurement-quality mechanism.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MeasurementRepairJob(Base):
    __tablename__ = "measurement_repair_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    order_log_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)

    # FILL_RECONCILIATION | LIFECYCLE_RECONCILIATION | OUTCOME_RECONCILIATION
    repair_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # PENDING | PROCESSING | COMPLETED | FAILED | MANUAL_REVIEW
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING", index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
