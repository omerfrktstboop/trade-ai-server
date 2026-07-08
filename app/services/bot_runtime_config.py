"""Bot-facing runtime config and metadata helpers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.risk_config import risk_config
from app.models.db import LockedPosition, SystemConfig, TradeProfile
from app.services.admin_config import (
    CONFIG_DEFINITIONS,
    list_admin_configs,
    public_config_keys,
)
from app.services.trade_profile import get_active_profile, get_static_default_profile


@dataclass(frozen=True)
class BotConfigMetadata:
    config_version: str
    config_hash: str


def _split_symbols(raw: str) -> list[str]:
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _parse_bool(raw_value: Any) -> bool:
    if isinstance(raw_value, bool):
        return raw_value
    return str(raw_value).strip().lower() in {"1", "true", "yes", "on"}


def _base_config_values(
    values: dict[str, str], locked_qty: dict[str, float], profile: TradeProfile
) -> dict[str, Any]:
    raw_mode = values["botMode"].upper()
    enable_real_orders = _parse_bool(values["botEnableRealOrders"])

    # Serving-time mode downgrade — first of two enforcement layers for
    # allow_real_live/allow_demo_live. The authoritative layer is the new
    # RiskEngine gate (app/services/risk_engine.py), which applies regardless
    # of whether the bot is running with fresh config; this one just stops a
    # freshly-(re)started bot from ever being TOLD to run in a mode its
    # active profile disallows.
    mode = raw_mode
    if mode == "REAL_LIVE" and not (profile.allow_real_live and enable_real_orders):
        mode = "PAPER"
    elif mode == "DEMO_LIVE" and not profile.allow_demo_live:
        mode = "PAPER"

    return {
        "mode": mode,
        "enableDemoOrders": _parse_bool(values["botEnableDemoOrders"]),
        "enableRealOrders": enable_real_orders,
        "requireDemoAccount": _parse_bool(values["botRequireDemoAccount"]),
        "demoAccountConfirmed": _parse_bool(values["botDemoAccountConfirmed"]),
        "maxOrderValueTl": float(profile.max_order_value_tl),
        "maxQtyPerOrder": float(profile.max_qty_per_order),
        "maxOrdersPerDay": int(profile.max_orders_per_day),
        "maxOrdersPerSymbolPerDay": int(profile.max_orders_per_symbol_per_day),
        # Defense in depth: even a corrupted DB value must not enable MARKET orders.
        "allowMarketOrders": False,
        "scanIntervalMinutes": int(profile.scan_interval_minutes),
        "httpTimeoutSeconds": int(values["botHttpTimeoutSeconds"]),
        "maxFetchLoopPerSession": int(profile.max_fetch_loop_per_session),
        "orderTimeInForce": profile.order_time_in_force,
        "indicatorPeriod": profile.indicator_period,
        "allowedSymbols": _split_symbols(values["allowedSymbols"]),
        "lockedLongTermQty": locked_qty,
    }


def _profile_summary(profile: TradeProfile) -> dict[str, str]:
    return {"code": profile.code, "name": profile.name, "riskLevel": profile.risk_level}


def _hash_config(config_values: dict[str, Any]) -> str:
    raw = json.dumps(
        config_values, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def _format_version(latest_updated_at: datetime | None, config_hash: str) -> str:
    if latest_updated_at is None:
        return f"static-default-{config_hash}"
    latest_updated_at = _as_utc(latest_updated_at)
    stamp = latest_updated_at.astimezone(UTC).replace(microsecond=0).isoformat()
    return f"{stamp.replace('+00:00', 'Z')}-{config_hash}"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def build_bot_runtime_config(session: AsyncSession) -> dict[str, Any]:
    items = {item.key: item.value for item in await list_admin_configs(session)}
    profile = await get_active_profile(session)
    locked_qty = await _load_locked_long_term_qty(
        session, _split_symbols(items["lockedLongTermSymbols"])
    )
    config_values = _base_config_values(items, locked_qty, profile)
    config_hash = _hash_config(config_values)
    latest_updated_at = await _latest_config_updated_at(session)
    return {
        "configVersion": _format_version(latest_updated_at, config_hash),
        "configHash": config_hash,
        "activeTradeProfile": _profile_summary(profile),
        **config_values,
    }


def build_static_bot_runtime_config() -> dict[str, Any]:
    values = {key: definition.default for key, definition in CONFIG_DEFINITIONS.items()}
    values.setdefault("allowedSymbols", risk_config.allowed_symbols)
    values.setdefault("lockedLongTermSymbols", risk_config.locked_long_term_symbols)
    locked_qty = {
        symbol: 0.0 for symbol in _split_symbols(values["lockedLongTermSymbols"])
    }
    profile = get_static_default_profile()
    config_values = _base_config_values(values, locked_qty, profile)
    config_hash = _hash_config(config_values)
    return {
        "configVersion": _format_version(None, config_hash),
        "configHash": config_hash,
        "activeTradeProfile": _profile_summary(profile),
        **config_values,
    }


async def get_bot_config_metadata(session: AsyncSession) -> BotConfigMetadata:
    config = await build_bot_runtime_config(session)
    return BotConfigMetadata(
        config_version=str(config["configVersion"]),
        config_hash=str(config["configHash"]),
    )


def get_static_bot_config_metadata() -> BotConfigMetadata:
    config = build_static_bot_runtime_config()
    return BotConfigMetadata(
        config_version=str(config["configVersion"]),
        config_hash=str(config["configHash"]),
    )


async def _load_locked_long_term_qty(
    session: AsyncSession, configured_symbols: list[str]
) -> dict[str, float]:
    result = {symbol: 0.0 for symbol in configured_symbols}
    rows = (
        await session.execute(
            select(LockedPosition).where(LockedPosition.lock_type == "LONG_TERM")
        )
    ).scalars().all()
    for row in rows:
        symbol = row.symbol.strip().upper()
        if not symbol:
            continue
        result[symbol] = float(row.qty or 0.0)
    return result


async def _latest_config_updated_at(session: AsyncSession) -> datetime | None:
    config_updated_at = (
        await session.execute(
            select(func.max(SystemConfig.updated_at)).where(
                SystemConfig.key.in_(public_config_keys())
            )
        )
    ).scalar_one_or_none()
    locked_created_at = (
        await session.execute(select(func.max(LockedPosition.created_at)))
    ).scalar_one_or_none()
    profile_updated_at = (
        await session.execute(select(func.max(TradeProfile.updated_at)))
    ).scalar_one_or_none()
    candidates = [
        dt for dt in (config_updated_at, locked_created_at, profile_updated_at)
        if dt is not None
    ]
    if not candidates:
        return None
    return max((_as_utc(dt) for dt in candidates))
