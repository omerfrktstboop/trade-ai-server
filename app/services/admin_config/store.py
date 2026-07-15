"""Admin config DB reads/writes: SystemConfig get/set + audit log, plus
the composite resolvers (is_kill_switch_enabled, get_trading_mode_override,
build_runtime_risk_config) that other services call as their single entry
point for DB-backed runtime config.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.risk_config import RiskConfig, risk_config
from app.models.db import ConfigAuditLog, SystemConfig
from app.models.signal import SignalMode
from app.services.trade_profile import get_active_profile

from app.services.admin_config.definitions import (
    CONFIG_DEFINITIONS,
    RISKY_CONFIRMATION,
    AdminConfigItem,
    public_config_keys,
)
from app.services.admin_config.validation import (
    _ensure_allowed_key,
    _parse_bool,
    _requires_confirmation,
    _serialize_value,
)


async def _load_config_rows(session: AsyncSession) -> dict[str, SystemConfig]:
    stmt = select(SystemConfig).where(SystemConfig.key.in_(public_config_keys()))
    rows = (await session.execute(stmt)).scalars().all()
    return {row.key: row for row in rows if row.key in CONFIG_DEFINITIONS}


async def list_admin_configs(session: AsyncSession) -> list[AdminConfigItem]:
    rows = await _load_config_rows(session)
    items: list[AdminConfigItem] = []
    for key, definition in CONFIG_DEFINITIONS.items():
        row = rows.get(key)
        value = row.value if row else definition.default
        items.append(
            AdminConfigItem(
                key=key,
                value=value,
                value_type=definition.value_type,
                description=definition.description,
                is_sensitive=definition.is_sensitive,
                source="db" if row else "default",
                updated_at=row.updated_at if row else None,
            )
        )
    return items


async def get_admin_config_value(session: AsyncSession, key: str) -> str:
    _ensure_allowed_key(key)
    stmt = select(SystemConfig).where(SystemConfig.key == key)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row:
        return row.value
    return CONFIG_DEFINITIONS[key].default


async def has_admin_config_row(session: AsyncSession, key: str) -> bool:
    _ensure_allowed_key(key)
    stmt = select(SystemConfig.id).where(SystemConfig.key == key)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def set_admin_config_value(
    session: AsyncSession,
    key: str,
    raw_value: Any,
    *,
    changed_by: str,
    reason: str | None = None,
    confirmation: str | None = None,
    commit: bool = True,
) -> AdminConfigItem:
    """Validate, persist, and audit one admin config value."""
    definition = _ensure_allowed_key(key)
    new_value = _serialize_value(key, raw_value, definition.value_type)
    old_value = await get_admin_config_value(session, key)

    if _requires_confirmation(key, old_value, new_value):
        if confirmation != RISKY_CONFIRMATION:
            raise ValueError(f"{key} requires confirmation={RISKY_CONFIRMATION}")

    stmt = select(SystemConfig).where(SystemConfig.key == key)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = SystemConfig(
            key=key,
            value=new_value,
            value_type=definition.value_type,
            description=definition.description,
            is_sensitive=definition.is_sensitive,
        )
        session.add(row)
    else:
        row.value = new_value
        row.value_type = definition.value_type
        row.description = definition.description
        row.is_sensitive = definition.is_sensitive

    if old_value != new_value:
        session.add(
            ConfigAuditLog(
                key=key,
                old_value=old_value,
                new_value=new_value,
                changed_by=changed_by,
                reason=reason or "Admin config update",
            )
        )

    if commit:
        await session.commit()
        if old_value != new_value:
            from app.services.decision_gate import decision_cache

            decision_cache.clear()
        await session.refresh(row)
    else:
        await session.flush()
    return AdminConfigItem(
        key=key,
        value=row.value,
        value_type=row.value_type,
        description=row.description or definition.description,
        is_sensitive=row.is_sensitive,
        source="db",
        updated_at=row.updated_at,
    )


async def set_admin_config_values(
    session: AsyncSession,
    values: dict[str, Any],
    *,
    changed_by: str,
    reason: str | None = None,
    confirmation: str | None = None,
) -> list[AdminConfigItem]:
    """Validate and persist a config snapshot in one DB transaction."""
    if not values:
        raise ValueError("At least one config value is required")
    items: list[AdminConfigItem] = []
    async with session.begin():
        for key, value in values.items():
            items.append(
                await set_admin_config_value(
                    session,
                    key,
                    value,
                    changed_by=changed_by,
                    reason=reason,
                    confirmation=confirmation,
                    commit=False,
                )
            )
    from app.services.decision_gate import decision_cache

    decision_cache.clear()
    return items


async def is_kill_switch_enabled(session: AsyncSession) -> bool:
    return (
        _parse_bool(await get_admin_config_value(session, "killSwitchEnabled"))
        or _parse_bool(await get_admin_config_value(session, "tradingKillSwitchActive"))
        or _parse_bool(await get_admin_config_value(session, "forceSafeMode"))
    )


async def get_trading_mode_override(session: AsyncSession) -> SignalMode | None:
    if not await has_admin_config_row(session, "tradingMode"):
        return None
    value = await get_admin_config_value(session, "tradingMode")
    return SignalMode(value.upper())


async def build_runtime_risk_config(session: AsyncSession) -> RiskConfig:
    """Build RiskConfig from the active trade profile + DB-backed admin
    config, falling back to code defaults where neither applies.

    Priority: active trade profile > per-field admin config override >
    static env default. Symbol lists, cutoff time, and timezone are NOT
    part of a trade profile — they stay admin-config-driven regardless.
    """
    values = {item.key: item.value for item in await list_admin_configs(session)}
    profile = await get_active_profile(session)
    bot_enable_real_orders = _parse_bool(values["botEnableRealOrders"])
    real_live_mode_allowed = _parse_bool(values["botRealLiveModeAllowed"])
    real_live_armed = _parse_bool(values["botRealLiveArmed"])
    return RiskConfig(
        allowed_symbols=values["allowedSymbols"],
        decline_symbols=values.get("declineSymbols", ""),
        locked_long_term_symbols=values["lockedLongTermSymbols"],
        max_position_value_per_symbol=profile.max_position_value_per_symbol,
        max_daily_trade_count=profile.max_orders_per_day,
        min_confidence_for_buy=profile.min_confidence_for_buy,
        min_confidence_for_sell=profile.min_confidence_for_sell,
        allow_sell_long_term=profile.allow_sell_long_term,
        allow_short_selling=profile.allow_short_selling,
        require_alpha_trend_alignment=profile.require_alpha_trend_alignment,
        require_indicator_consensus_alignment=(
            profile.require_indicator_consensus_alignment
        ),
        min_indicator_consensus_count=risk_config.min_indicator_consensus_count,
        max_natr_for_buy=profile.max_natr_for_buy,
        max_depth_queue_drop_pct_for_buy=profile.max_depth_queue_drop_pct_for_buy,
        max_spread_pct_for_buy=profile.max_spread_pct_for_buy,
        min_depth_bid_ask_ratio_top10_for_buy=profile.min_depth_bid_ask_ratio_top10_for_buy,
        max_depth_sell_pressure_score_for_buy=profile.max_depth_sell_pressure_score_for_buy,
        block_buy_on_strong_sell_pressure=profile.block_buy_on_strong_sell_pressure,
        block_buy_on_near_ask_wall=profile.block_buy_on_near_ask_wall,
        near_wall_distance_pct=profile.near_wall_distance_pct,
        real_live_mode_allowed=(
            profile.allow_real_live
            and bot_enable_real_orders
            and real_live_mode_allowed
            and real_live_armed
        ),
        demo_live_mode_allowed=profile.allow_demo_live,
        disable_trading_after=values["disableTradingAfter"],
        timezone=values["timezone"],
        _env_file="",
    )
