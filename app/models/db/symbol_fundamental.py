"""Symbol fundamentals — admin-entered quarterly balance-sheet summary.

One row per symbol, overwritten each quarter (no history — the AI only
needs the latest snapshot). No free API exists for BIST fundamentals, and
the data only changes quarterly, so manual admin entry is the pragmatic
real data source (vs. the fund/broker placeholders that were disabled).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SymbolFundamental(Base):
    __tablename__ = "symbol_fundamentals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(16), unique=True, index=True, nullable=False
    )

    # Reporting period the numbers refer to, e.g. "2026/Q2".
    period: Mapped[str] = mapped_column(String(16), nullable=False)

    # Free cash flow growth vs. prior period, percent.
    fcf_growth_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Leverage: total debt / equity.
    debt_to_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Net profit margin, percent.
    net_margin_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Net margin change vs. prior period, percentage points (expansion > 0).
    net_margin_change_pt: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Revenue growth vs. prior period, percent.
    revenue_growth_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    updated_by: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
