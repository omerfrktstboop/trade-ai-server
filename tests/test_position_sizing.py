from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.services.effective_risk_config import EffectiveRiskConfig
from app.services.position_sizing import (
    AccountSizingContext,
    PositionSizingService,
    TradeSizingContext,
)


D = Decimal


def limits(**overrides) -> EffectiveRiskConfig:
    values = {
        "risk_per_trade_pct": D("1"),
        "max_cash_utilization_pct": D("100"),
        "max_account_exposure_pct": D("100"),
        "max_position_value_per_symbol": D("1000000"),
        "max_order_value_tl": D("1000000"),
        "max_qty_per_order": 100000,
        "min_order_value_tl": D("1"),
        "min_stop_distance_pct": D("0.1"),
        "max_stop_distance_pct": D("20"),
        "minimum_stop_slippage_pct": D("0"),
        "maximum_stop_slippage_pct": D("5"),
        "profile_stop_slippage_pct": D("0"),
        "max_account_data_age_seconds": D("60"),
        "minimum_buy_confidence": D("70"),
        "minimum_sell_confidence": D("70"),
        "daily_order_limit": 100,
        "per_symbol_daily_order_limit": 100,
        "sizing_enabled": True,
        "demo_orders_enabled": True,
        "real_orders_enabled": False,
        "environment_config_fingerprint": "a" * 64,
        "system_config_version": "test-v1",
        "trade_profile_id": 1,
        "trade_profile_code": "TEST",
        "trade_profile_version": 1,
    }
    values.update(overrides)
    return EffectiveRiskConfig(**values)


def account(**overrides) -> AccountSizingContext:
    values = {
        "account_equity_tl": "100000",
        "effective_available_cash_tl": "100000",
        "reserved_cash_tl": "0",
        "current_symbol_qty": 0,
        "current_symbol_value_tl": "0",
        "total_account_exposure_tl": "0",
        "account_data_age_seconds": "1",
        "account_data_reliable": True,
    }
    values.update(overrides)
    return AccountSizingContext(**values)


def trade(**overrides) -> TradeSizingContext:
    values = {
        "symbol": "THYAO",
        "entry_price": "100",
        "stop_loss": "95",
        "target_price": "110",
        "confidence": "82",
        "current_price": "100",
    }
    values.update(overrides)
    return TradeSizingContext(**values)


def calculate(*, a=None, t=None, risk_limits=None):
    return PositionSizingService().calculate_buy_size(
        account=a or account(),
        trade=t or trade(),
        limits=risk_limits or limits(),
    )


def test_normal_risk_calculation_and_invariant():
    result = calculate()
    assert result.allowed
    assert result.qty == 200
    assert result.estimated_loss_at_stop_tl == result.risk_budget_tl
    assert result.binding_limits == ["risk_budget"]


@pytest.mark.parametrize(
    ("account_overrides", "limit_overrides", "expected", "binding"),
    [
        ({"effective_available_cash_tl": "250"}, {}, 2, "cash_budget"),
        (
            {"current_symbol_value_tl": "900"},
            {"max_position_value_per_symbol": D("1000")},
            1,
            "symbol_position",
        ),
        (
            {"total_account_exposure_tl": "9900"},
            {"max_account_exposure_pct": D("10")},
            1,
            "account_exposure",
        ),
        ({}, {"max_order_value_tl": D("300")}, 3, "order_value"),
        ({}, {"max_qty_per_order": 4}, 4, "profile_max_qty"),
    ],
)
def test_binding_limits(account_overrides, limit_overrides, expected, binding):
    result = calculate(
        a=account(**account_overrides),
        risk_limits=limits(**limit_overrides),
    )
    assert result.allowed
    assert result.qty == expected
    assert binding in result.binding_limits


def test_too_narrow_and_too_wide_stop_are_blocked():
    assert not calculate(t=trade(stop_loss="99.95")).allowed
    assert not calculate(t=trade(stop_loss="70")).allowed


def test_slippage_reduces_quantity():
    without = calculate(risk_limits=limits(profile_stop_slippage_pct=D("0")))
    with_buffer = calculate(risk_limits=limits(profile_stop_slippage_pct=D("1")))
    assert with_buffer.qty < without.qty
    assert with_buffer.slippage_buffer_tl == D("1")


def test_stale_unreliable_and_unknown_exposure_fail_closed():
    assert not calculate(a=account(account_data_age_seconds="61")).allowed
    assert not calculate(a=account(account_data_reliable=False)).allowed
    assert not calculate(a=account(total_account_exposure_tl=None)).allowed


def test_zero_lot_and_minimum_order_value_are_blocked():
    result = calculate(a=account(effective_available_cash_tl="0.50"))
    assert not result.allowed
    assert result.qty == 0
    result = calculate(
        risk_limits=limits(min_order_value_tl=D("1000"), max_qty_per_order=1)
    )
    assert not result.allowed


def test_decimal_precision_and_json_round_trip_use_strings():
    result = calculate(
        t=trade(entry_price="100.123456789", stop_loss="95.123456788"),
        risk_limits=limits(profile_stop_slippage_pct=D("0.123456789")),
    )
    assert isinstance(result.order_value_tl, Decimal)
    payload = json.loads(result.model_dump_json())
    assert isinstance(payload["order_value_tl"], str)
    restored = type(result).model_validate_json(result.model_dump_json())
    assert restored.order_value_tl == result.order_value_tl
    assert restored.effective_stop_distance_tl == result.effective_stop_distance_tl


def test_external_float_is_converted_through_its_string_form():
    context = TradeSizingContext(
        symbol="THYAO",
        entry_price=0.1,
        stop_loss=0.09,
        target_price=0.2,
        confidence=82.1,
        current_price=0.1,
    )
    assert context.entry_price == D("0.1")
    assert context.confidence == D("82.1")
