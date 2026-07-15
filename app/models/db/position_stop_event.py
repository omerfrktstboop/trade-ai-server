"""Position stop event — audit trail of every stop-loss change/breach/reject
on a PositionLifecycle (Task 4.4). Append-only; never mutated or deleted.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PositionStopEvent(Base):
    __tablename__ = "position_stop_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_lifecycle_id: Mapped[int] = mapped_column(
        ForeignKey("position_lifecycles.id"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    old_stop: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    new_stop: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)

    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
