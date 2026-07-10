"""Server-authoritative runtime configuration for the Matriks gateway.

Fail-closed guarantees enforced here (the gateway re-checks its own hard
limits on top):

- ``mode`` is downgraded to ``PAPER`` when the active trade profile does not
  allow the configured ``botMode`` (e.g. ``REAL_LIVE`` while the profile has
  ``allow_real_live=False``). A misconfigured admin panel can never leak a
  live mode to the gateway past its profile.
- ``configHash`` fingerprints the full response so the gateway (and tests)
  can cheaply detect "did anything change?" across polls.
"""

import hashlib
import json

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.config import settings
from app.core.auth import verify_token
from app.db.session import async_session_factory
from app.models.db import BotPosition, LockedPosition, WatchlistSymbol
from app.services.admin_config import list_admin_configs
from app.services.trade_profile import get_active_profile

router = APIRouter(tags=["Gateway"], dependencies=[Depends(verify_token)])

_LIVE_REAL_MODES = {"REAL_LIVE", "LIVE"}
_LIVE_DEMO_MODES = {"DEMO_LIVE"}


def _effective_mode(bot_mode: str, profile) -> str:
    """Downgrade the configured mode to PAPER when the profile disallows it."""
    mode = (bot_mode or "PAPER").strip().upper()
    if mode in _LIVE_REAL_MODES and not profile.allow_real_live:
        return "PAPER"
    if mode in _LIVE_DEMO_MODES and not profile.allow_demo_live:
        return "PAPER"
    return mode


@router.get("/gateway/config")
async def gateway_runtime_config() -> dict:
    """Return the complete fail-closed configuration consumed by Matriks."""
    async with async_session_factory() as session:
        values = {item.key: item.value for item in await list_admin_configs(session)}
        profile = await get_active_profile(session)
        portfolio = (await session.execute(select(BotPosition))).scalars().all()
        locked = (await session.execute(select(LockedPosition))).scalars().all()
        watchlist = (
            await session.execute(
                select(WatchlistSymbol.symbol).where(
                    WatchlistSymbol.is_active.is_(True)
                )
            )
        ).scalars().all()

    symbols = {
        value.strip().upper()
        for value in values["allowedSymbols"].split(",")
        if value.strip()
    }
    symbols.update(row.symbol.strip().upper() for row in portfolio if row.qty > 0)
    # Data-only abonelikler: emir yolu RiskEngine'in allowedSymbols
    # kontrolünden geçtiği için bunlara emir gidemez; gateway yalnızca
    # snapshot/movers verisi sağlar.
    #   - Makro filtre endeksi (XU100)
    #   - Discovery keşif evreni (movers ranking'i genişletir)
    #   - Aktif watchlist adayları (scanner analizi için snapshot gerekir)
    if settings.market_index_symbol.strip():
        symbols.add(settings.market_index_symbol.strip().upper())
    symbols.update(
        s.strip().upper() for s in settings.discovery_symbols.split(",") if s.strip()
    )
    symbols.update(str(s).strip().upper() for s in watchlist)
    locked_qty: dict[str, float] = {}
    for row in locked:
        symbol = row.symbol.strip().upper()
        locked_qty[symbol] = locked_qty.get(symbol, 0.0) + float(row.qty)

    config = {
        "ok": True,
        "symbols": sorted(symbols),
        "lockedLongTermQty": locked_qty,
        "mode": _effective_mode(values["botMode"], profile),
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
        "scanIntervalMinutes": profile.scan_interval_minutes,
        "profileCode": profile.code,
        "activeTradeProfile": {
            "code": profile.code,
            "name": profile.name,
            "riskLevel": profile.risk_level,
        },
    }
    config["configHash"] = hashlib.sha256(
        json.dumps(config, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return config
