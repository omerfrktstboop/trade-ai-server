"""Gerçek order/lifecycle olay sayaçları (Plan Faz 3.1).

Plan bölüm 8: günlük sayaç, risk tarafından onaylanan karar sayısını değil,
gerçek order ve lifecycle olaylarını saymalıdır — ``RiskDecision.allow_order``
true olup emre dönüşmeyen kararlar kapasiteyi tüketmez. Bu modül birbirinden
ayrı yedi sayacı ``order_logs`` + ``position_lifecycles`` + ``exit_intents``
üzerinden üretir; hiçbiri ``risk_decisions``'a bakmaz.

Salt-okuma; trade akışına dokunmaz.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import ExitIntent, OrderLog, PositionLifecycle

# Gateway'e ulaşmış (kabul edilmiş) sayılan emir statüleri: rezervasyonun
# ötesine geçip broker'a gönderilmiş olanlar.
_ACCEPTED_STATUSES = ("SENT_PENDING", "NEW", "PARTIALLY_FILLED", "FILLED")


async def _count_orders(
    session: AsyncSession,
    *,
    action: str,
    since: datetime | None,
    statuses: tuple[str, ...] | None = None,
) -> int:
    stmt = select(func.count(OrderLog.id)).where(OrderLog.action == action)
    if since is not None:
        stmt = stmt.where(OrderLog.created_at >= since)
    if statuses is not None:
        stmt = stmt.where(OrderLog.status.in_(statuses))
    return int((await session.execute(stmt)).scalar_one() or 0)


async def build_event_counters(
    session: AsyncSession, since: datetime | None
) -> dict[str, Any]:
    """Yedi ayrı gerçek-olay sayacını döndür (plan bölüm 8)."""
    entry_intent = await _count_orders(session, action="BUY", since=since)
    entry_accepted = await _count_orders(
        session, action="BUY", since=since, statuses=_ACCEPTED_STATUSES
    )
    entry_filled = await _count_orders(
        session, action="BUY", since=since, statuses=("FILLED",)
    )
    exit_accepted = await _count_orders(
        session, action="SELL", since=since, statuses=_ACCEPTED_STATUSES
    )
    exit_filled = await _count_orders(
        session, action="SELL", since=since, statuses=("FILLED",)
    )

    lifecycle_stmt = select(func.count(PositionLifecycle.id)).where(
        PositionLifecycle.status == "CLOSED"
    )
    if since is not None:
        lifecycle_stmt = lifecycle_stmt.where(PositionLifecycle.closed_at >= since)
    completed_lifecycles = int((await session.execute(lifecycle_stmt)).scalar_one() or 0)

    # Cancel/reprice denemesi: exit niyetlerindeki reprice jenerasyonlarının
    # toplamı (her cancel/reprice generation'ı 1 artırır).
    reprice_stmt = select(
        func.coalesce(func.sum(ExitIntent.cancel_reprice_generation), 0)
    )
    if since is not None:
        reprice_stmt = reprice_stmt.where(ExitIntent.created_at >= since)
    cancel_reprice_attempts = int((await session.execute(reprice_stmt)).scalar_one() or 0)

    return {
        "entryIntent": entry_intent,
        "entryOrderAccepted": entry_accepted,
        "entryOrderFilled": entry_filled,
        "exitOrderAccepted": exit_accepted,
        "exitOrderFilled": exit_filled,
        "completedLifecycles": completed_lifecycles,
        "cancelRepriceAttempts": cancel_reprice_attempts,
    }
