"""Admin config value validation: per-type serialization/parsing and the
CONFIRM-required-change rules (which key changes are risky enough to
require an explicit confirmation token).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from app.models.signal import SignalMode

from app.services.admin_config.definitions import (
    CONFIG_DEFINITIONS,
    SECRET_CONFIG_KEYS,
    RISKY_CONFIG_KEYS,
    ConfigDefinition,
)


def _ensure_allowed_key(key: str) -> ConfigDefinition:
    if key in SECRET_CONFIG_KEYS or key not in CONFIG_DEFINITIONS:
        raise ValueError(f"Unsupported admin config key: {key}")
    definition = CONFIG_DEFINITIONS[key]
    if definition.is_sensitive:
        raise ValueError(f"Sensitive admin config key cannot be exposed: {key}")
    return definition


def _serialize_value(key: str, raw_value: Any, value_type: str) -> str:
    if value_type == "bool":
        value = _parse_bool(raw_value)
        if key == "botAllowMarketOrders" and value:
            raise ValueError(
                "botAllowMarketOrders=true is not allowed; MARKET orders are disabled"
            )
        return str(value).lower()
    if value_type == "int":
        value = int(raw_value)
        if value < 0:
            raise ValueError(f"{key} must be >= 0")
        if key == "marketDataWarningRateLimitSeconds" and not 1 <= value <= 3600:
            raise ValueError(f"{key} must be between 1 and 3600")
        return str(value)
    if value_type == "float":
        value = float(raw_value)
        if value < 0:
            raise ValueError(f"{key} must be >= 0")
        return str(value)
    if value_type == "decimal":
        try:
            value = (
                raw_value if isinstance(raw_value, Decimal) else Decimal(str(raw_value))
            )
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"{key} must be a decimal number") from exc
        if not value.is_finite() or value < 0:
            raise ValueError(f"{key} must be a finite value >= 0")
        if key == "marketDataDiagnosticSampleRatePct" and value > 100:
            raise ValueError(f"{key} must be <= 100")
        return str(value)
    if value_type == "mode":
        value = str(raw_value).upper()
        SignalMode(value)
        return value
    if value_type == "system_mode":
        value = str(raw_value).strip().upper()
        if value not in {"OBSERVE_ONLY", "AUTO_TRADE"}:
            raise ValueError(f"{key} must be OBSERVE_ONLY or AUTO_TRADE")
        return value
    if value_type == "reservation_handling":
        value = str(raw_value).strip().upper()
        if value not in {
            "BROKER_ALREADY_DEDUCTED",
            "BACKEND_DEDUCTED",
            "UNKNOWN",
        }:
            raise ValueError(f"{key} has an invalid reservation handling policy")
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
    if key in {
        "allowedSymbols",
        "declineSymbols",
        "buyAllowedSymbols",
        "sellExitAllowedSymbols",
        "lockedLongTermSymbols",
        "scanUniverseSymbols",
    }:
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
        return (
            new_value
            in {
                SignalMode.LIVE.value,
                SignalMode.DEMO_LIVE.value,
                SignalMode.REAL_LIVE.value,
            }
            and old_value != new_value
        )
    if key == "systemMode":
        # AUTO_TRADE'e geçiş gerçek emir yolunu kurar — onay ister.
        return new_value == "AUTO_TRADE" and old_value != new_value
    if key == "realAccountArmed":
        return _parse_bool(new_value) is True and old_value != new_value
    if key in {"killSwitchEnabled", "tradingKillSwitchActive", "forceSafeMode"}:
        return _parse_bool(old_value) is True and _parse_bool(new_value) is False
    if key == "scannerEnabled":
        # Kapatmak stop-loss bekçisi dahil tüm otomasyonu durdurur — onay ister.
        return _parse_bool(old_value) is True and _parse_bool(new_value) is False
    if key in {"scannerAllowOrders", "manualApprovalAllowOrders"}:
        # Açmak gerçek emir yolunu kurar — onay ister.
        return _parse_bool(new_value) is True and old_value != new_value
    if key == "botMode":
        return (
            new_value
            in {
                SignalMode.DEMO_LIVE.value,
                SignalMode.REAL_LIVE.value,
            }
            and old_value != new_value
        )
    if key in {
        "botEnableRealOrders",
        "botRealLiveModeAllowed",
        "botRealLiveArmed",
        "botDemoAccountConfirmed",
        "sizingAllowMarginBuying",
    }:
        return _parse_bool(new_value) is True and old_value != new_value
    if key == "botRequireDemoAccount":
        return _parse_bool(old_value) is True and _parse_bool(new_value) is False
    if key == "accountReservationHandling":
        return old_value == "UNKNOWN" and new_value != "UNKNOWN"
    increase_is_risky = {
        "sizingRiskPerTradePct",
        "sizingMaxCashUtilizationPct",
        "sizingMaxAccountExposurePct",
        "sizingMaxPositionValuePerSymbol",
        "sizingMaxOrderValueTl",
        "sizingMaxQtyPerOrder",
        "sizingMaxStopDistancePct",
        "sizingMaxAccountDataAgeSeconds",
        "sizingDailyOrderLimit",
        "sizingPerSymbolDailyOrderLimit",
    }
    decrease_is_risky = {
        "sizingMinStopDistancePct",
        "sizingMinimumStopSlippagePct",
        "sizingMaximumStopSlippagePct",
        "sizingProfileStopSlippagePct",
        "sizingMinimumBuyConfidence",
        "sizingMinimumSellConfidence",
    }
    old_decimal = Decimal(old_value)
    new_decimal = Decimal(new_value)
    if key in increase_is_risky:
        return new_decimal > old_decimal
    if key in decrease_is_risky:
        return new_decimal < old_decimal
    return False
