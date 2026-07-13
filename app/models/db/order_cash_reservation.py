"""Durable cash reservations preventing concurrent BUY over-allocation."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OrderCashReservation(Base):
    __tablename__ = "order_cash_reservations"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_order_cash_reservations_request_id"),
        CheckConstraint("reserved_qty >= 0", name="ck_cash_reservation_qty_nonnegative"),
        CheckConstraint(
            "remaining_qty >= 0 AND remaining_qty <= reserved_qty",
            name="ck_cash_reservation_remaining_qty",
        ),
        CheckConstraint("limit_price > 0", name="ck_cash_reservation_price_positive"),
        CheckConstraint(
            "reserved_amount_tl >= 0", name="ck_cash_reservation_amount_nonnegative"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    reserved_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    reserved_amount_tl: Mapped[Decimal] = mapped_column(
        Numeric(28, 10), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
