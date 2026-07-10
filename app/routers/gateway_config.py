"""Server-authoritative runtime configuration for the Matriks gateway."""

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.core.auth import verify_token
from app.db.session import async_session_factory
from app.models.db import BotPosition, LockedPosition
from app.services.admin_config import list_admin_configs
from app.services.trade_profile import get_active_profile

router = APIRouter(tags=["Gateway"], dependencies=[Depends(verify_token)])


@router.get("/gateway/config")
async def gateway_runtime_config() -> dict:
    """Return the complete fail-closed configuration consumed by Matriks."""
    async with async_session_factory() as session:
        values = {item.key: item.value for item in await list_admin_configs(session)}
        profile = await get_active_profile(session)
        portfolio = (await session.execute(select(BotPosition))).scalars().all()
        locked = (await session.execute(select(LockedPosition))).scalars().all()

    symbols = {
        value.strip().upper()
        for value in values["allowedSymbols"].split(",")
        if value.strip()
    }
    symbols.update(row.symbol.strip().upper() for row in portfolio if row.qty > 0)
    locked_qty: dict[str, float] = {}
    for row in locked:
        symbol = row.symbol.strip().upper()
        locked_qty[symbol] = locked_qty.get(symbol, 0.0) + float(row.qty)

    return {
        "ok": True,
        "symbols": sorted(symbols),
        "lockedLongTermQty": locked_qty,
        "mode": values["botMode"],
        "enableDemoOrders": values["botEnableDemoOrders"] == "true",
        "enableRealOrders": values["botEnableRealOrders"] == "true",
        "requireDemoAccount": values["botRequireDemoAccount"] == "true",
        "demoAccountConfirmed": values["botDemoAccountConfirmed"] == "true",
        "maxOrderValueTl": profile.max_order_value_tl,
        "maxQtyPerOrder": profile.max_qty_per_order,
        "maxOrdersPerDay": profile.max_orders_per_day,
        "maxOrdersPerSymbolPerDay": profile.max_orders_per_symbol_per_day,
        "orderTimeInForce": profile.order_time_in_force,
        "indicatorPeriod": profile.indicator_period,
        "profileCode": profile.code,
    }
