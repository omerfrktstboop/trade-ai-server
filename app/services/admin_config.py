"""Runtime admin configuration backed by ``system_configs``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.risk_config import RiskConfig, risk_config
from app.models.db import ConfigAuditLog, SystemConfig
from app.models.signal import SignalMode
from app.services.trade_profile import get_active_profile


SECRET_CONFIG_KEYS = {"API_TOKEN", "DEEPSEEK_API_KEY", "DATABASE_URL"}
RISKY_CONFIRMATION = "CONFIRM"


@dataclass(frozen=True)
class ConfigDefinition:
    key: str
    value_type: str
    default: str
    description: str
    is_sensitive: bool = False


def _settings_default_mode() -> str:
    return str(settings.default_mode.value).upper()


CONFIG_DEFINITIONS: dict[str, ConfigDefinition] = {
    "allowedSymbols": ConfigDefinition(
        "allowedSymbols",
        "string",
        risk_config.allowed_symbols,
        "İşlem yapılmasına izin verilen semboller. Virgülle ayrılmış liste kullanın.",
    ),
    "lockedLongTermSymbols": ConfigDefinition(
        "lockedLongTermSymbols",
        "string",
        risk_config.locked_long_term_symbols,
        "Uzun vadeli kilitli semboller. Bu semboller otomatik SELL kararlarından korunur.",
    ),
    "maxPositionValuePerSymbol": ConfigDefinition(
        "maxPositionValuePerSymbol",
        "float",
        str(risk_config.max_position_value_per_symbol),
        "Bir sembolde botun alabileceği maksimum pozisyon değeri.",
    ),
    "maxDailyTradeCount": ConfigDefinition(
        "maxDailyTradeCount",
        "int",
        str(risk_config.max_daily_trade_count),
        "Gün içinde izin verilen maksimum BUY/SELL işlem sayısı.",
    ),
    "minConfidenceForBuy": ConfigDefinition(
        "minConfidenceForBuy",
        "float",
        str(risk_config.min_confidence_for_buy),
        "BUY kararı için gereken minimum AI güven skoru.",
    ),
    "minConfidenceForSell": ConfigDefinition(
        "minConfidenceForSell",
        "float",
        str(risk_config.min_confidence_for_sell),
        "SELL kararı için gereken minimum AI güven skoru.",
    ),
    "disableTradingAfter": ConfigDefinition(
        "disableTradingAfter",
        "time",
        risk_config.disable_trading_after,
        "Bu saatten sonra BUY/SELL kararları engellenir. Format HH:MM.",
    ),
    "timezone": ConfigDefinition(
        "timezone",
        "timezone",
        risk_config.timezone,
        "İşlem kesim saati kontrollerinde kullanılan IANA timezone değeri.",
    ),
    "tradingMode": ConfigDefinition(
        "tradingMode",
        "mode",
        _settings_default_mode(),
        "Gelen request mode değerini sistem genelinde override eden çalışma modu.",
    ),
    "killSwitchEnabled": ConfigDefinition(
        "killSwitchEnabled",
        "bool",
        "false",
        "true olduğunda tüm sinyal değerlendirmeleri WAIT ve allowOrder=false döner.",
    ),
    "allowSellLongTerm": ConfigDefinition(
        "allowSellLongTerm",
        "bool",
        str(risk_config.allow_sell_long_term).lower(),
        "Uzun vadeli kilitli semboller için otomatik SELL kararına izin verir.",
    ),
    "botMode": ConfigDefinition(
        "botMode",
        "mode",
        "PAPER",
        "Matriks bot runtime modu. Riskli modlar confirmation ister.",
    ),
    "botEnableDemoOrders": ConfigDefinition(
        "botEnableDemoOrders",
        "bool",
        "false",
        "Matriks botun demo hesaba emir gondermesine izin verir.",
    ),
    "botEnableRealOrders": ConfigDefinition(
        "botEnableRealOrders",
        "bool",
        "false",
        "Matriks botun real hesaba emir gondermesine izin verir.",
    ),
    "botRequireDemoAccount": ConfigDefinition(
        "botRequireDemoAccount",
        "bool",
        "true",
        "Demo hesap onayi zorunlulugunu belirler.",
    ),
    "botDemoAccountConfirmed": ConfigDefinition(
        "botDemoAccountConfirmed",
        "bool",
        "false",
        "Matriks demo hesap kullanildigini onaylar.",
    ),
    "botMaxOrderValueTl": ConfigDefinition(
        "botMaxOrderValueTl",
        "float",
        "1000",
        "Matriks bot tek emir maksimum TL degeri.",
    ),
    "botMaxQtyPerOrder": ConfigDefinition(
        "botMaxQtyPerOrder",
        "float",
        "1",
        "Matriks bot tek emir maksimum adet.",
    ),
    "botMaxOrdersPerDay": ConfigDefinition(
        "botMaxOrdersPerDay",
        "int",
        "3",
        "Matriks bot gunluk maksimum emir sayisi.",
    ),
    "botMaxOrdersPerSymbolPerDay": ConfigDefinition(
        "botMaxOrdersPerSymbolPerDay",
        "int",
        "1",
        "Matriks bot sembol basina gunluk maksimum emir sayisi.",
    ),
    "botAllowMarketOrders": ConfigDefinition(
        "botAllowMarketOrders",
        "bool",
        "false",
        "MARKET emirleri sistem genelinde yasaktir; true kabul edilmez.",
    ),
    "botScanIntervalMinutes": ConfigDefinition(
        "botScanIntervalMinutes",
        "int",
        "30",
        "Matriks bot tarama araligi, dakika.",
    ),
    "botHttpTimeoutSeconds": ConfigDefinition(
        "botHttpTimeoutSeconds",
        "int",
        "15",
        "Matriks bot HTTP timeout suresi, saniye.",
    ),
    "botMaxFetchLoopPerSession": ConfigDefinition(
        "botMaxFetchLoopPerSession",
        "int",
        "3",
        "Agentic FETCH_DATA dongusu icin maksimum tur sayisi.",
    ),
    "botOrderTimeInForce": ConfigDefinition(
        "botOrderTimeInForce",
        "time_in_force",
        "Day",
        "Matriks limit emir gecerlilik tipi: Day veya GoodTillCancel.",
    ),
    "botIndicatorPeriod": ConfigDefinition(
        "botIndicatorPeriod",
        "symbol_period",
        "Min5",
        "Matriks indikator periyodu: Min, Min5, Min15, Min30, Hour veya Day.",
    ),
}

RISKY_CONFIG_KEYS = {
    "tradingMode",
    "killSwitchEnabled",
    "allowSellLongTerm",
    "botMode",
    "botEnableRealOrders",
    "botDemoAccountConfirmed",
    "botMaxOrderValueTl",
    "botMaxQtyPerOrder",
    "botMaxOrdersPerDay",
    "botMaxOrdersPerSymbolPerDay",
}


@dataclass(frozen=True)
class AdminConfigItem:
    key: str
    value: str
    value_type: str
    description: str
    is_sensitive: bool
    source: str
    updated_at: datetime | None = None

    @property
    def display_value(self) -> str:
        if self.is_sensitive:
            return "********"
        return self.value


def public_config_keys() -> list[str]:
    """Return non-secret config keys in stable display order."""
    return list(CONFIG_DEFINITIONS)


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

    await session.commit()
    await session.refresh(row)
    return AdminConfigItem(
        key=key,
        value=row.value,
        value_type=row.value_type,
        description=row.description or definition.description,
        is_sensitive=row.is_sensitive,
        source="db",
        updated_at=row.updated_at,
    )


async def is_kill_switch_enabled(session: AsyncSession) -> bool:
    value = await get_admin_config_value(session, "killSwitchEnabled")
    return _parse_bool(value)


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
    return RiskConfig(
        allowed_symbols=values["allowedSymbols"],
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
        real_live_mode_allowed=profile.allow_real_live and bot_enable_real_orders,
        demo_live_mode_allowed=profile.allow_demo_live,
        disable_trading_after=values["disableTradingAfter"],
        timezone=values["timezone"],
        _env_file="",
    )


def _ensure_allowed_key(key: str) -> ConfigDefinition:
    if key in SECRET_CONFIG_KEYS or key not in CONFIG_DEFINITIONS:
        raise ValueError(f"Unsupported admin config key: {key}")
    definition = CONFIG_DEFINITIONS[key]
    if definition.is_sensitive:
        raise ValueError(f"Sensitive admin config key cannot be exposed: {key}")
    return definition


async def _load_config_rows(session: AsyncSession) -> dict[str, SystemConfig]:
    stmt = select(SystemConfig).where(SystemConfig.key.in_(public_config_keys()))
    rows = (await session.execute(stmt)).scalars().all()
    return {row.key: row for row in rows if row.key in CONFIG_DEFINITIONS}


def _serialize_value(key: str, raw_value: Any, value_type: str) -> str:
    if value_type == "bool":
        value = _parse_bool(raw_value)
        if key == "botAllowMarketOrders" and value:
            raise ValueError("botAllowMarketOrders=true is not allowed; MARKET orders are disabled")
        return str(value).lower()
    if value_type == "int":
        value = int(raw_value)
        if value < 0:
            raise ValueError(f"{key} must be >= 0")
        return str(value)
    if value_type == "float":
        value = float(raw_value)
        if value < 0:
            raise ValueError(f"{key} must be >= 0")
        return str(value)
    if value_type == "mode":
        value = str(raw_value).upper()
        SignalMode(value)
        return value
    if value_type == "time":
        value = str(raw_value).strip()
        hour, minute = value.split(":", 1)
        if len(hour) != 2 or len(minute) != 2:
            raise ValueError(f"{key} must be HH:MM")
        hour_int = int(hour)
        minute_int = int(minute)
        if hour_int < 0 or hour_int > 23 or minute_int < 0 or minute_int > 59:
            raise ValueError(f"{key} must be HH:MM")
        return f"{hour_int:02d}:{minute_int:02d}"
    if value_type == "timezone":
        value = str(raw_value).strip()
        ZoneInfo(value)
        return value
    if value_type == "time_in_force":
        value = str(raw_value).strip()
        normalized = value.replace("_", "").replace("-", "").replace(" ", "").lower()
        if normalized in {"day", "d"}:
            return "Day"
        if normalized in {"gtc", "goodtillcancel", "goodtilcancel"}:
            return "GoodTillCancel"
        raise ValueError(f"{key} must be Day or GoodTillCancel")
    if value_type == "symbol_period":
        value = str(raw_value).strip()
        allowed = {
            "min": "Min",
            "min5": "Min5",
            "min15": "Min15",
            "min30": "Min30",
            "hour": "Hour",
            "day": "Day",
        }
        normalized = value.replace("_", "").replace("-", "").replace(" ", "").lower()
        if normalized not in allowed:
            raise ValueError(f"{key} must be one of Min, Min5, Min15, Min30, Hour, Day")
        return allowed[normalized]

    value = str(raw_value).strip()
    if key in {"allowedSymbols", "lockedLongTermSymbols"}:
        return ",".join(
            symbol.strip().upper() for symbol in value.split(",") if symbol.strip()
        )
    return value


def _parse_bool(raw_value: Any) -> bool:
    if isinstance(raw_value, bool):
        return raw_value
    value = str(raw_value).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"Invalid boolean value: {raw_value}")


def _requires_confirmation(key: str, old_value: str, new_value: str) -> bool:
    if key not in RISKY_CONFIG_KEYS:
        return False
    if key == "tradingMode":
        return new_value in {
            SignalMode.LIVE.value,
            SignalMode.DEMO_LIVE.value,
            SignalMode.REAL_LIVE.value,
        } and old_value != new_value
    if key == "killSwitchEnabled":
        return _parse_bool(old_value) is True and _parse_bool(new_value) is False
    if key == "allowSellLongTerm":
        return _parse_bool(new_value) is True and old_value != new_value
    if key == "botMode":
        return new_value in {
            SignalMode.DEMO_LIVE.value,
            SignalMode.REAL_LIVE.value,
        } and old_value != new_value
    if key in {"botEnableRealOrders", "botDemoAccountConfirmed"}:
        return _parse_bool(new_value) is True and old_value != new_value
    if key in {
        "botMaxOrderValueTl",
        "botMaxQtyPerOrder",
        "botMaxOrdersPerDay",
        "botMaxOrdersPerSymbolPerDay",
    }:
        return float(new_value) > float(old_value)
    return False
