"""Watchlist symbols — discovery agent'ın bulduğu dinamik izleme adayları.

``allowedSymbols`` (admin-config, elle yönetilen işlem evreni) ile
karıştırılmamalı: watchlist, movers taramasından geçen adayların scanner
tarafından ANALİZ edilmesini sağlar; emir yolu yine RiskEngine'in
allowedSymbols kontrolünden geçer.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WatchlistSymbol(Base):
    __tablename__ = "watchlist_symbols"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(16), unique=True, index=True, nullable=False
    )

    # Aday nereden geldi: GAINER / LOSER / VOLUME_LEADER
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # Elemelerden geçtiği andaki gerekçe (insan-okur özet).
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)

    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
