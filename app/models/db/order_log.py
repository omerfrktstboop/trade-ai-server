"""Order log — record of every order sent to the exchange."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OrderLog(Base):
    __tablename__ = "order_logs"
    __table_args__ = (UniqueConstraint("request_id", name="uq_order_logs_request_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True, nullable=False)

    action: Mapped[str] = mapped_column(String(8), nullable=False)
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    order_qty: Mapped[float] = mapped_column(Float, default=0.0)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    state: Mapped[str] = mapped_column(String(32), default="RESERVED")

    # v2: mod kavramı kaldırıldı; kolon geçmiş kayıtlar/gösterim için kalır ve
    # dispatch edilen emirlerde "AUTO_TRADE" ile doldurulur.
    mode: Mapped[str] = mapped_column(String(16), default="OBSERVE_ONLY")
    order_type: Mapped[str] = mapped_column(String(16), default="LIMIT")
    # Emir GÖNDERİM anında damgalanan hesap referansı (sha256). Callback fill'i
    # bu sabit değeri kullanır — callback anındaki canlı hesabı DEĞİL — böylece
    # emir sonrası hesap değişse bile fill doğru hesaba yazılır (Fix #1).
    account_ref: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    decision_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rounded_limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    last_fill_qty: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    reservation_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    send_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    profile_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    matrix_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
