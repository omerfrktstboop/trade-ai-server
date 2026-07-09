"""Fundamentals service — admin-entered balance-sheet context for the AI.

Reads the ``symbol_fundamentals`` table (one row per symbol, entered
quarterly via the admin panel) and serializes it into the
``fundamentalsContext`` payload field. Distinct from the disabled
``fundContext`` (mutual-fund holdings) and ``brokerFlowContext``
(institutional flow) placeholders — this one has a REAL data source.

Symbols without an entered row are simply absent from the context (no
empty/UNKNOWN placeholders — consistent with the fund/broker disable
decision: never feed the AI structured noise). Any DB error degrades to
an empty context; fundamentals are a decision input, never something
that should block an evaluation. That also covers prod environments
where the table hasn't been created yet (no migration system).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.db import SymbolFundamental

logger = logging.getLogger(__name__)

# Editable numeric fields, shared by the admin form parser and the API.
NUMERIC_FIELDS = (
    "fcf_growth_pct",
    "debt_to_equity",
    "net_margin_pct",
    "net_margin_change_pt",
    "revenue_growth_pct",
)


# ── AI-facing context ─────────────────────────────────────────────────────────


async def get_fundamentals_context(symbols: list[str]) -> dict[str, Any]:
    """Return admin-entered fundamentals per symbol for the AI payload.

    Only symbols with an actual row appear in the result.
    """
    normalized = [s.strip().upper() for s in symbols if s.strip()]
    if not normalized:
        return {}

    try:
        async with async_session_factory() as session:
            stmt = select(SymbolFundamental).where(
                SymbolFundamental.symbol.in_(normalized)
            )
            rows = (await session.execute(stmt)).scalars().all()
    except Exception:
        logger.exception("Failed to load fundamentals context for %s", normalized)
        return {}

    return {row.symbol: _serialize(row) for row in rows}


def _serialize(row: SymbolFundamental) -> dict[str, Any]:
    return {
        "period": row.period,
        "fcfGrowthPct": row.fcf_growth_pct,
        "debtToEquity": row.debt_to_equity,
        "netMarginPct": row.net_margin_pct,
        "netMarginChangePt": row.net_margin_change_pt,
        "revenueGrowthPct": row.revenue_growth_pct,
        "notes": row.notes,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


# ── Admin CRUD ────────────────────────────────────────────────────────────────


async def list_fundamentals(session: AsyncSession) -> list[SymbolFundamental]:
    stmt = select(SymbolFundamental).order_by(SymbolFundamental.symbol.asc())
    return list((await session.execute(stmt)).scalars().all())


async def get_fundamental(
    session: AsyncSession, symbol: str
) -> SymbolFundamental | None:
    stmt = select(SymbolFundamental).where(
        SymbolFundamental.symbol == symbol.strip().upper()
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def upsert_fundamental(
    session: AsyncSession,
    symbol: str,
    *,
    period: str,
    changed_by: str,
    notes: str | None = None,
    **numeric_fields: float | None,
) -> SymbolFundamental:
    """Create or update the single fundamentals row for a symbol."""
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    period = period.strip()
    if not period:
        raise ValueError("period is required (e.g. 2026/Q2)")
    for key in numeric_fields:
        if key not in NUMERIC_FIELDS:
            raise ValueError(f"Unknown fundamentals field: {key}")

    row = await get_fundamental(session, symbol)
    if row is None:
        row = SymbolFundamental(symbol=symbol, period=period, updated_by=changed_by)
        session.add(row)
    else:
        row.period = period
        row.updated_by = changed_by

    row.notes = notes
    for key in NUMERIC_FIELDS:
        # Fields omitted from the call are cleared — the admin form always
        # posts the full row, so absence means "blank", not "keep old".
        setattr(row, key, numeric_fields.get(key))

    await session.commit()
    await session.refresh(row)
    return row


async def delete_fundamental(session: AsyncSession, symbol: str) -> bool:
    """Remove a symbol's fundamentals row. Returns True if one existed."""
    row = await get_fundamental(session, symbol)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True
