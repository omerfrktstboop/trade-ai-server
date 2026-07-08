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
from app.models.db import LockedPosition, SystemConfig
from app.services.admin_config import (
    CONFIG_DEFINITIONS,
    list_admin_configs,
    public_config_keys,
)


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
    values: dict[str, str], locked_qty: dict[str, float]
) -> dict[str, Any]:
    return {
        "mode": values["botMode"].upper(),
        "enableDemoOrders": _parse_bool(values["botEnableDemoOrders"]),
        "enableRealOrders": _parse_bool(values["botEnableRealOrders"]),
        "requireDemoAccount": _parse_bool(values["botRequireDemoAccount"]),
        "demoAccountConfirmed": _parse_bool(values["botDemoAccountConfirmed"]),
        "maxOrderValueTl": float(values["botMaxOrderValueTl"]),
        "maxQtyPerOrder": float(values["botMaxQtyPerOrder"]),
        "maxOrdersPerDay": int(values["botMaxOrdersPerDay"]),
        "maxOrdersPerSymbolPerDay": int(values["botMaxOrdersPerSymbolPerDay"]),
        # Defense in depth: even a corrupted DB value must not enable MARKET orders.
        "allowMarketOrders": False,
        "scanIntervalMinutes": int(values["botScanIntervalMinutes"]),
        "httpTimeoutSeconds": int(values["botHttpTimeoutSeconds"]),
        "maxFetchLoopPerSession": int(values["botMaxFetchLoopPerSession"]),
        "orderTimeInForce": values["botOrderTimeInForce"],
        "indicatorPeriod": values["botIndicatorPeriod"],
        "allowedSymbols": _split_symbols(values["allowedSymbols"]),
        "lockedLongTermQty": locked_qty,
    }


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
    locked_qty = await _load_locked_long_term_qty(
        session, _split_symbols(items["lockedLongTermSymbols"])
    )
    config_values = _base_config_values(items, locked_qty)
    config_hash = _hash_config(config_values)
    latest_updated_at = await _latest_config_updated_at(session)
    return {
        "configVersion": _format_version(latest_updated_at, config_hash),
        "configHash": config_hash,
        **config_values,
    }


def build_static_bot_runtime_config() -> dict[str, Any]:
    values = {key: definition.default for key, definition in CONFIG_DEFINITIONS.items()}
    values.setdefault("allowedSymbols", risk_config.allowed_symbols)
    values.setdefault("lockedLongTermSymbols", risk_config.locked_long_term_symbols)
    locked_qty = {
        symbol: 0.0 for symbol in _split_symbols(values["lockedLongTermSymbols"])
    }
    config_values = _base_config_values(values, locked_qty)
    config_hash = _hash_config(config_values)
    return {
        "configVersion": _format_version(None, config_hash),
        "configHash": config_hash,
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
    candidates = [dt for dt in (config_updated_at, locked_created_at) if dt is not None]
    if not candidates:
        return None
    return max((_as_utc(dt) for dt in candidates))
