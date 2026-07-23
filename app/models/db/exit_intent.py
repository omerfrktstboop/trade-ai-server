"""ExitIntent — bir pozisyon çıkışının kalıcı niyet + durum kaydı (Plan Faz 2.2).

Deterministik exit monitörü bir çıkış tetiklediğinde bir ExitIntent yazar:
neden, tetik zamanı/fiyatı, uygulanan politika versiyonu, ilişkili
request/order kimliği ve emrin yaşam döngüsü durumu. Bu, çıkışların
denetlenebilir olmasını ve cancel/reprice mantığının (Faz 2.3) hangi çıkış
emirlerinin hâlâ açık olduğunu bilmesini sağlar.

Durum makinesi: ACCEPTED -> (PARTIAL) -> FILLED | CANCELED | FAILED. Bir
cancel/reprice her denendiğinde ``cancel_reprice_generation`` artar.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ExitIntent(Base):
    __tablename__ = "exit_intents"
    __table_args__ = (
        Index("ix_exit_intents_symbol_status", "symbol", "status"),
        Index("ix_exit_intents_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_lifecycle_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    # HARD_TARGET | STOP | BREAKEVEN | TRAILING | STAGNATION | MAX_HOLD
    exit_reason: Mapped[str] = mapped_column(String(24), nullable=False)
    trigger_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10), nullable=True)
    trigger_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)

    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ACCEPTED | PARTIAL | FILLED | CANCELED | FAILED
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACCEPTED")
    cancel_reprice_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
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
