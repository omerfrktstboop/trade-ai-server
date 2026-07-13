"""Operational audit for broker account normalization decisions."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Integer, JSON, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AccountNormalizationAudit(Base):
    __tablename__ = "account_normalization_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), index=True)
    source_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    source_fields: Mapped[dict] = mapped_column(JSON, nullable=False)
    normalization_policy: Mapped[str] = mapped_column(String(128), nullable=False)
    reservation_handling: Mapped[str] = mapped_column(String(32), nullable=False)
    account_data_reliable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    unreliable_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    account_data_age_seconds: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    margin_buying_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    broker_reported_buying_power_tl: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 10)
    )
    backend_reserved_cash_tl: Mapped[Decimal] = mapped_column(
        Numeric(28, 10), nullable=False
    )
    effective_available_cash_tl: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 10)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
