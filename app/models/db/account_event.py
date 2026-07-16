"""Hesap kimliği/arming olay günlüğü (v2 Faz 4).

Her arm/disarm işlemi ve watcher'ın tespit ettiği her hesap kimliği, hesap
türü, oturum veya kontrat değişikliği buraya yazılır. Hesap kimliği yalnızca
gateway'in ürettiği sha256 referansı olarak saklanır — ham id asla.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AccountEvent(Base):
    __tablename__ = "account_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # ARMED | DISARMED | ACCOUNT_CHANGED | TYPE_CHANGED | SESSION_CHANGED |
    # CONTRACT_MISMATCH
    event_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    account_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    account_session_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    account_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    previous_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # ADMIN | WATCHER | GATEWAY
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
