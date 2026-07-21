"""Account-scoped bot ownership derived only from backend-reserved fills."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import OrderLog


@dataclass(frozen=True)
class BotOwnershipSnapshot:
    quantities: dict[str, Decimal]
    average_costs: dict[str, Decimal]


async def load_bot_ownership(
    session: AsyncSession, account_ref: str
) -> BotOwnershipSnapshot:
    normalized_ref = account_ref.strip()
    if len(normalized_ref) != 64:
        raise ValueError("verified account_ref is required for bot ownership")
    orders = (
        (
            await session.execute(
                select(OrderLog).where(
                    OrderLog.filled_qty > 0,
                    OrderLog.request_fingerprint.is_not(None),
                    OrderLog.account_ref == normalized_ref,
                )
            )
        )
        .scalars()
        .all()
    )
    bought_qty: dict[str, Decimal] = {}
    sold_qty: dict[str, Decimal] = {}
    bought_cost: dict[str, Decimal] = {}
    for order in orders:
        symbol = order.symbol.strip().upper()
        filled = Decimal(str(order.filled_qty or 0))
        if filled <= 0:
            continue
        if str(order.action).upper() == "BUY":
            price = Decimal(
                str(
                    order.avg_price
                    or order.rounded_limit_price
                    or order.limit_price
                    or 0
                )
            )
            if price <= 0:
                raise ValueError(f"bot BUY fill price is unavailable for {symbol}")
            bought_qty[symbol] = bought_qty.get(symbol, Decimal("0")) + filled
            bought_cost[symbol] = bought_cost.get(symbol, Decimal("0")) + filled * price
        elif str(order.action).upper() == "SELL":
            sold_qty[symbol] = sold_qty.get(symbol, Decimal("0")) + filled

    quantities: dict[str, Decimal] = {}
    average_costs: dict[str, Decimal] = {}
    for symbol, total_bought in bought_qty.items():
        net = max(Decimal("0"), total_bought - sold_qty.get(symbol, Decimal("0")))
        if net <= 0:
            continue
        quantities[symbol] = net
        average_costs[symbol] = bought_cost[symbol] / total_bought
    return BotOwnershipSnapshot(quantities=quantities, average_costs=average_costs)
