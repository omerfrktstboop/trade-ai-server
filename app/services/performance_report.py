"""Read-only decision and order performance aggregates."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.db import OrderLog, RiskDecision
from app.services.block_reason_classifier import classify_block_reason


def range_start(value: str) -> datetime | None:
    hours = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}.get(value)
    return datetime.now(timezone.utc) - timedelta(hours=hours) if hours else None


async def build_performance_report(
    range_value: str = "7d", symbol: str | None = None
) -> dict[str, Any]:
    since = range_start(range_value)
    async with async_session_factory() as session:
        risks_stmt = select(RiskDecision)
        orders_stmt = select(OrderLog)
        if since:
            risks_stmt = risks_stmt.where(RiskDecision.created_at >= since)
            orders_stmt = orders_stmt.where(OrderLog.created_at >= since)
        if symbol:
            symbol = symbol.upper()
            risks_stmt = risks_stmt.where(RiskDecision.symbol == symbol)
            orders_stmt = orders_stmt.where(OrderLog.symbol == symbol)
        risks = list((await session.execute(risks_stmt)).scalars().all())
        orders = list((await session.execute(orders_stmt)).scalars().all())
    actions = Counter(row.action for row in risks)
    categories = Counter(
        classify_block_reason(row.reason) for row in risks if not row.allow_order
    )
    statuses = Counter(row.status.upper() for row in orders)
    by_symbol = Counter(row.symbol for row in risks)
    return {
        "range": range_value,
        "symbol": symbol,
        "totalDecisions": len(risks),
        "buyCount": actions["BUY"],
        "sellCount": actions["SELL"],
        "waitCount": actions["WAIT"],
        "allowedOrders": sum(row.allow_order for row in risks),
        "blockedDecisions": sum(not row.allow_order for row in risks),
        "topBlockReason": categories.most_common(1)[0][0] if categories else "-",
        "topSymbols": by_symbol.most_common(10),
        "averageConfidence": round(sum(row.confidence for row in risks) / len(risks), 2)
        if risks
        else 0,
        "averageRiskScore": round(sum(row.risk_score for row in risks) / len(risks), 2)
        if risks
        else 0,
        "orderStatuses": dict(statuses),
        "ordersSent": statuses["SENT_PENDING"],
        "filledOrders": statuses["FILLED"],
        "rejectedOrders": statuses["REJECTED"],
        "errorOrders": statuses["ERROR"],
        "estimatedRealizedPnl": _estimated_pnl(orders),
        "pnlExperimental": True,
        "latestDecisions": risks[-50:],
    }


def _estimated_pnl(orders: list[OrderLog]) -> float:
    costs: dict[str, tuple[float, float]] = {}
    pnl = 0.0
    for row in sorted(
        (r for r in orders if r.status.upper() == "FILLED"), key=lambda r: r.created_at
    ):
        qty, price = row.qty, row.price or 0.0
        held, cost = costs.get(row.symbol, (0.0, 0.0))
        if row.action.upper() == "BUY":
            costs[row.symbol] = (
                held + qty,
                ((held * cost) + qty * price) / (held + qty),
            )
        elif row.action.upper() == "SELL" and held:
            sold = min(held, qty)
            pnl += sold * (price - cost)
            costs[row.symbol] = (held - sold, cost)
    return round(pnl, 2)
