from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.effective_risk_config import (
    EffectiveRiskConfigResolver,
    EnvironmentRiskLimits,
    SystemRiskConfig,
)
from app.services.admin_config import _requires_confirmation, _serialize_value
from app.services.trade_profile import profile_requires_confirmation


def profile(**overrides):
    values = {
        "id": 9,
        "code": "CUSTOM",
        "version": 4,
        "risk_per_trade_pct": "4",
        "max_cash_utilization_pct": "90",
        "max_account_exposure_pct": "90",
        "max_position_value_per_symbol": "90000",
        "max_order_value_tl": "90000",
        "max_qty_per_order": 900,
        "min_order_value_tl": "1",
        "min_stop_distance_pct": "0.01",
        "max_stop_distance_pct": "40",
        "minimum_stop_slippage_pct": "0.01",
        "maximum_stop_slippage_pct": "4",
        "profile_stop_slippage_pct": "0.01",
        "max_account_data_age_seconds": "900",
        "min_confidence_for_buy": "10",
        "min_confidence_for_sell": "10",
        "max_orders_per_day": 900,
        "max_orders_per_symbol_per_day": 900,
        "allow_demo_live": True,
        "allow_real_live": True,
        "allow_margin_buying": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_profile_cannot_relax_environment_or_system_limits():
    env = EnvironmentRiskLimits()
    system = SystemRiskConfig(
        risk_per_trade_pct="0.40",
        min_stop_distance_pct="0.20",
        minimum_buy_confidence="80",
    )
    effective = EffectiveRiskConfigResolver().resolve(
        environment_limits=env, system_config=system, trade_profile=profile()
    )
    assert effective.risk_per_trade_pct == Decimal("0.40")
    assert effective.max_order_value_tl == Decimal("1000")
    assert effective.max_qty_per_order == 3
    assert effective.min_stop_distance_pct == Decimal("0.20")
    assert effective.minimum_buy_confidence == Decimal("80")
    assert not effective.real_orders_enabled


def test_boolean_permissions_require_every_layer():
    effective = EffectiveRiskConfigResolver().resolve(
        environment_limits=EnvironmentRiskLimits(demo_orders_enabled=True),
        system_config=SystemRiskConfig(demo_orders_enabled=False),
        trade_profile=profile(allow_demo_live=True),
    )
    assert not effective.demo_orders_enabled


def test_margin_buying_requires_environment_system_and_profile_permission():
    resolver = EffectiveRiskConfigResolver()
    allowed = resolver.resolve(
        environment_limits=EnvironmentRiskLimits(allow_margin_buying=True),
        system_config=SystemRiskConfig(allow_margin_buying=True),
        trade_profile=profile(allow_margin_buying=True),
    )
    assert allowed.allow_margin_buying is True

    blocked = resolver.resolve(
        environment_limits=EnvironmentRiskLimits(allow_margin_buying=False),
        system_config=SystemRiskConfig(allow_margin_buying=True),
        trade_profile=profile(allow_margin_buying=True),
    )
    assert blocked.allow_margin_buying is False


def test_profile_cannot_reduce_global_slippage_buffer():
    effective = EffectiveRiskConfigResolver().resolve(
        environment_limits=EnvironmentRiskLimits(profile_stop_slippage_pct="0.30"),
        system_config=SystemRiskConfig(profile_stop_slippage_pct="0.40"),
        trade_profile=profile(profile_stop_slippage_pct="0.01"),
    )
    assert effective.profile_stop_slippage_pct == Decimal("0.40")


def test_environment_fingerprint_is_deterministic_and_secret_free():
    first = EnvironmentRiskLimits().fingerprint()
    second = EnvironmentRiskLimits().fingerprint()
    assert first == second
    assert len(first) == 64


def test_risk_increasing_system_config_changes_require_confirmation():
    assert _requires_confirmation("sizingRiskPerTradePct", "0.5", "0.6")
    assert _requires_confirmation("sizingMinStopDistancePct", "0.2", "0.1")
    assert _requires_confirmation("sizingProfileStopSlippagePct", "0.3", "0.2")
    assert _requires_confirmation("sizingMaxAccountDataAgeSeconds", "60", "90")
    assert not _requires_confirmation("sizingMaxOrderValueTl", "1000", "900")
    assert _requires_confirmation("sizingAllowMarginBuying", "false", "true")
    assert _requires_confirmation(
        "accountReservationHandling", "UNKNOWN", "BACKEND_DEDUCTED"
    )


def test_reservation_policy_config_rejects_unknown_values():
    assert (
        _serialize_value(
            "accountReservationHandling",
            "broker_already_deducted",
            "reservation_handling",
        )
        == "BROKER_ALREADY_DEDUCTED"
    )
    with pytest.raises(ValueError):
        _serialize_value(
            "accountReservationHandling", "guess", "reservation_handling"
        )


def test_risk_increasing_trade_profile_changes_require_confirmation():
    current = profile()
    assert profile_requires_confirmation(current, {"risk_per_trade_pct": "5"})
    assert profile_requires_confirmation(current, {"min_stop_distance_pct": "0"})
    assert profile_requires_confirmation(current, {"profile_stop_slippage_pct": "0"})
    assert profile_requires_confirmation(current, {"allow_margin_buying": True})
