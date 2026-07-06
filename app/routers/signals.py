"""Latest signals endpoint — returns recent risk decisions for dashboard/history."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import verify_token
from app.db.session import get_async_session
from app.models.db import RiskDecision

router = APIRouter(tags=["Signals"], dependencies=[Depends(verify_token)])


@router.get("/signals/latest")
async def latest_signals(
    symbol: str | None = Query(None, description="Optional symbol filter (e.g. THYAO)"),
    db: AsyncSession = Depends(get_async_session),
) -> list[dict]:
    """Return the 20 most recent risk decisions, optionally filtered by symbol."""
    stmt = select(RiskDecision).order_by(RiskDecision.created_at.desc()).limit(20)
    if symbol:
        stmt = stmt.where(RiskDecision.symbol == symbol.upper())

    result = await db.execute(stmt)
    rows = result.scalars().all()

    return [
        {
            "id": r.id,
            "requestId": r.request_id,
            "symbol": r.symbol,
            "action": r.action,
            "qty": r.qty,
            "orderType": r.order_type,
            "confidenceScore": r.confidence,
            "riskScore": r.risk_score,
            "allowOrder": r.allow_order,
            "reason": r.reason,
            "entryMin": r.entry_min,
            "entryMax": r.entry_max,
            "stopLoss": r.stop_loss,
            "targetPrice": r.target_price,
            "mode": r.mode,
            "createdAt": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
