"""Central, fail-closed resolution of position-sizing risk limits.

All financial values stay as :class:`~decimal.Decimal`.  The resolver applies
the strictest value from environment, system configuration and the active
trade profile; a profile can therefore never relax a global safety boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


_DECIMAL_FIELDS = {
    "risk_per_trade_pct",
    "total_bot_capital_budget_tl",
    "max_cash_utilization_pct",
    "max_account_exposure_pct",
    "max_position_value_per_symbol",
    "max_order_value_tl",
    "min_order_value_tl",
    "min_stop_distance_pct",
    "max_stop_distance_pct",
    "minimum_stop_slippage_pct",
    "maximum_stop_slippage_pct",
    "profile_stop_slippage_pct",
    "max_account_data_age_seconds",
    "minimum_buy_confidence",
    "minimum_sell_confidence",
}

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def decimal_from_external(value: Any) -> Decimal:
    """Convert an external scalar without ever constructing Decimal(float)."""
    if isinstance(value, Decimal):
        result = value
    else:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"Invalid decimal value: {value!r}") from exc
    if not result.is_finite():
        raise ValueError("Decimal value must be finite")
    return result


class RiskConfigLayer(BaseModel):
    """Common shape shared by all three risk configuration layers."""

    model_config = ConfigDict(frozen=True)

    risk_per_trade_pct: Decimal = Decimal("0.50")
    total_bot_capital_budget_tl: Decimal = Decimal("0")
    max_cash_utilization_pct: Decimal = Decimal("25")
    max_account_exposure_pct: Decimal = Decimal("50")
    max_position_value_per_symbol: Decimal = Decimal("3000")
    max_order_value_tl: Decimal = Decimal("1000")
    max_qty_per_order: int = 3
    min_order_value_tl: Decimal = Decimal("1")
    min_stop_distance_pct: Decimal = Decimal("0.10")
    max_stop_distance_pct: Decimal = Decimal("10")
    minimum_stop_slippage_pct: Decimal = Decimal("0.05")
    maximum_stop_slippage_pct: Decimal = Decimal("1")
    profile_stop_slippage_pct: Decimal = Decimal("0.20")
    max_account_data_age_seconds: Decimal = Decimal("60")
    minimum_buy_confidence: Decimal = Decimal("75")
    minimum_sell_confidence: Decimal = Decimal("70")
    daily_order_limit: int = 3
    per_symbol_daily_order_limit: int = 1
    sizing_enabled: bool = True
    demo_orders_enabled: bool = True
    real_orders_enabled: bool = False
    allow_margin_buying: bool = False

    @field_validator(*sorted(_DECIMAL_FIELDS), mode="before")
    @classmethod
    def _parse_decimal(cls, value: Any) -> Decimal:
        return decimal_from_external(value)

    @field_validator(*sorted(_DECIMAL_FIELDS))
    @classmethod
    def _non_negative_decimal(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("Risk configuration values cannot be negative")
        return value

    @field_validator(
        "max_qty_per_order", "daily_order_limit", "per_symbol_daily_order_limit"
    )
    @classmethod
    def _positive_integer(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("Risk limit must be a positive integer")
        return value


class EnvironmentRiskLimits(RiskConfigLayer):
    """Hard safety ceiling sourced from environment variables."""

    @classmethod
    def from_environment(cls) -> "EnvironmentRiskLimits":
        dotenv = dotenv_values(_ENV_FILE)
        values: dict[str, Any] = {}
        for name in cls.model_fields:
            env_name = f"RISK_{name.upper()}"
            value = os.environ.get(env_name)
            if value is None:
                value = dotenv.get(env_name)
            if value is not None:
                values[name] = value
        return cls.model_validate(values)

    def fingerprint(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SystemRiskConfig(RiskConfigLayer):
    """Typed view of centrally managed system configuration."""

    version: str = "defaults-v1"


class EffectiveRiskConfig(RiskConfigLayer):
    """Immutable result consumed by evaluator, sizing and RiskEngine."""

    environment_config_fingerprint: str
    system_config_version: str
    trade_profile_id: int | None = None
    trade_profile_code: str
    trade_profile_version: int


class EffectiveRiskConfigResolver:
    """Resolve the strictest configuration across all authority layers."""

    _MAXIMUM_LIMITS = {
        "risk_per_trade_pct",
        "max_cash_utilization_pct",
        "max_account_exposure_pct",
        "max_position_value_per_symbol",
        "max_order_value_tl",
        "max_qty_per_order",
        "max_stop_distance_pct",
        "max_account_data_age_seconds",
        "maximum_stop_slippage_pct",
        "daily_order_limit",
        "per_symbol_daily_order_limit",
    }
    _MINIMUM_SAFETY = {
        "min_order_value_tl",
        "minimum_buy_confidence",
        "minimum_sell_confidence",
        "min_stop_distance_pct",
        "minimum_stop_slippage_pct",
    }
    _BOOLEAN_PERMISSIONS = {
        "sizing_enabled",
        "demo_orders_enabled",
        "real_orders_enabled",
        "allow_margin_buying",
    }

    def resolve(
        self,
        *,
        environment_limits: EnvironmentRiskLimits,
        system_config: SystemRiskConfig,
        trade_profile: Any,
    ) -> EffectiveRiskConfig:
        profile = self._profile_layer(trade_profile)
        layers = (environment_limits, system_config, profile)
        resolved: dict[str, Any] = {}
        for field in self._MAXIMUM_LIMITS:
            resolved[field] = min(getattr(layer, field) for layer in layers)
        for field in self._MINIMUM_SAFETY:
            resolved[field] = max(getattr(layer, field) for layer in layers)
        for field in self._BOOLEAN_PERMISSIONS:
            resolved[field] = all(getattr(layer, field) for layer in layers)

        # The total bot budget is an explicit operator allocation, not a
        # trade-profile preference. Zero deliberately closes new BUY sizing.
        resolved["total_bot_capital_budget_tl"] = (
            system_config.total_bot_capital_budget_tl
        )

        # The requested profile buffer is a preference, constrained by the
        # effective global minimum/maximum envelope.
        requested_slippage = max(layer.profile_stop_slippage_pct for layer in layers)
        resolved["profile_stop_slippage_pct"] = min(
            resolved["maximum_stop_slippage_pct"],
            max(resolved["minimum_stop_slippage_pct"], requested_slippage),
        )
        if resolved["min_stop_distance_pct"] > resolved["max_stop_distance_pct"]:
            raise ValueError("Effective minimum stop distance exceeds maximum")
        if (
            resolved["minimum_stop_slippage_pct"]
            > resolved["maximum_stop_slippage_pct"]
        ):
            raise ValueError("Effective minimum slippage exceeds maximum")

        return EffectiveRiskConfig(
            **resolved,
            environment_config_fingerprint=environment_limits.fingerprint(),
            system_config_version=system_config.version,
            trade_profile_id=getattr(trade_profile, "id", None),
            trade_profile_code=str(getattr(trade_profile, "code", "UNKNOWN")),
            trade_profile_version=int(getattr(trade_profile, "version", 1) or 1),
        )

    @staticmethod
    def _profile_layer(profile: Any) -> RiskConfigLayer:
        mapping = {
            "risk_per_trade_pct": "risk_per_trade_pct",
            "max_cash_utilization_pct": "max_cash_utilization_pct",
            "max_account_exposure_pct": "max_account_exposure_pct",
            "max_position_value_per_symbol": "max_position_value_per_symbol",
            "max_order_value_tl": "max_order_value_tl",
            "max_qty_per_order": "max_qty_per_order",
            "min_order_value_tl": "min_order_value_tl",
            "min_stop_distance_pct": "min_stop_distance_pct",
            "max_stop_distance_pct": "max_stop_distance_pct",
            "minimum_stop_slippage_pct": "minimum_stop_slippage_pct",
            "maximum_stop_slippage_pct": "maximum_stop_slippage_pct",
            "profile_stop_slippage_pct": "profile_stop_slippage_pct",
            "max_account_data_age_seconds": "max_account_data_age_seconds",
            "minimum_buy_confidence": "min_confidence_for_buy",
            "minimum_sell_confidence": "min_confidence_for_sell",
            "daily_order_limit": "max_orders_per_day",
            "per_symbol_daily_order_limit": "max_orders_per_symbol_per_day",
            "demo_orders_enabled": "allow_demo_live",
            "real_orders_enabled": "allow_real_live",
            "allow_margin_buying": "allow_margin_buying",
        }
        values: dict[str, Any] = {}
        defaults = RiskConfigLayer()
        for target, source in mapping.items():
            value = getattr(profile, source, None)
            values[target] = getattr(defaults, target) if value is None else value
        values["sizing_enabled"] = getattr(profile, "sizing_enabled", True)
        return RiskConfigLayer.model_validate(values)


_SYSTEM_CONFIG_KEYS = {
    "risk_per_trade_pct": "sizingRiskPerTradePct",
    "total_bot_capital_budget_tl": "sizingTotalBotCapitalBudgetTl",
    "max_cash_utilization_pct": "sizingMaxCashUtilizationPct",
    "max_account_exposure_pct": "sizingMaxAccountExposurePct",
    "max_position_value_per_symbol": "sizingMaxPositionValuePerSymbol",
    "max_order_value_tl": "sizingMaxOrderValueTl",
    "max_qty_per_order": "sizingMaxQtyPerOrder",
    "min_order_value_tl": "sizingMinOrderValueTl",
    "min_stop_distance_pct": "sizingMinStopDistancePct",
    "max_stop_distance_pct": "sizingMaxStopDistancePct",
    "minimum_stop_slippage_pct": "sizingMinimumStopSlippagePct",
    "maximum_stop_slippage_pct": "sizingMaximumStopSlippagePct",
    "profile_stop_slippage_pct": "sizingProfileStopSlippagePct",
    "max_account_data_age_seconds": "sizingMaxAccountDataAgeSeconds",
    "minimum_buy_confidence": "sizingMinimumBuyConfidence",
    "minimum_sell_confidence": "sizingMinimumSellConfidence",
    "daily_order_limit": "sizingDailyOrderLimit",
    "per_symbol_daily_order_limit": "sizingPerSymbolDailyOrderLimit",
    "allow_margin_buying": "sizingAllowMarginBuying",
}


async def resolve_effective_risk_config(
    session: AsyncSession,
) -> EffectiveRiskConfig:
    """Load one immutable effective snapshot for a complete evaluation."""
    from app.models.db import SystemConfig
    from app.services.trade_profile import get_active_profile

    keys = set(_SYSTEM_CONFIG_KEYS.values())
    rows = (
        (await session.execute(select(SystemConfig).where(SystemConfig.key.in_(keys))))
        .scalars()
        .all()
    )
    by_key = {row.key: row for row in rows}
    values: dict[str, Any] = {}
    versions: list[str] = []
    for field, key in _SYSTEM_CONFIG_KEYS.items():
        row = by_key.get(key)
        if row is not None:
            values[field] = row.value
            if row.updated_at is not None:
                versions.append(row.updated_at.isoformat())
    values["version"] = max(versions, default="defaults-v1")
    profile = await get_active_profile(session)
    return EffectiveRiskConfigResolver().resolve(
        environment_limits=EnvironmentRiskLimits.from_environment(),
        system_config=SystemRiskConfig.model_validate(values),
        trade_profile=profile,
    )
