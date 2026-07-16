"""Order fill — one row per real, individually-recorded gateway fill delta.

Distinct from OrderLog (which stores the order's current cumulative state):
a BUY/SELL order that fills in three partial gateway callbacks produces one
OrderLog row but three OrderFill rows, each carrying only the *new* quantity
and derived price for that callback. This is the source-of-truth ledger for
net P&L (Task 1) - it must never be fabricated from OrderLog.qty/price.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OrderFill(Base):
    __tablename__ = "order_fills"
    __table_args__ = (
        UniqueConstraint("fill_event_key", name="uq_order_fills_fill_event_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_log_id: Mapped[int] = mapped_column(
        ForeignKey("order_logs.id"), nullable=False, index=True
    )
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(8), nullable=False)

    # Hesap referansı (sha256): fill'in ait olduğu hesap. DEMO ve REAL
    # fill'lerinin günlük PnL'de karışmasını önler (Fix #4). Fill anında aktif
    # hesabın accountRef'iyle damgalanır; bilinmiyorsa None.
    account_ref: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    fill_qty: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)

    gross_value_tl: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    commission_tl: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    exchange_fee_tl: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    other_fee_tl: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    total_cost_tl: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)

    # None when limit_price is unknown - never a fabricated zero (Task 1.4).
    slippage_tl: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    slippage_pct: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)

    # Idempotency fingerprint: hash of (request_id, cumulative filled_qty at
    # the time of this callback, cumulative avg_price). A duplicate gateway
    # retry recomputes the same key and is rejected by the unique constraint.
    fill_event_key: Mapped[str] = mapped_column(String(128), nullable=False)

    # CALLBACK_DELTA: created inline from a gateway order-result callback.
    # RECONCILIATION: created later by measurement_reconciliation.py to
    # recover a fill delta that the callback-time SAVEPOINT lost (Task 1.1).
    fill_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="CALLBACK_DELTA"
    )

    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
