from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.account_context import MatriksAccountContextAdapter


def account_payload(**account_overrides):
    account = {
        "TotalEquity": "100000.1234567890",
        "OrderableCash": "50000.9876543210",
        "CashBalance": "40000.00",
        "T1Balance": "9000.00",
        "T2Balance": "8000.00",
        "CreditLimit": "25000.00",
    }
    account.update(account_overrides)
    return {
        "ok": True,
        "sourceProvider": "MATRIKS_IQ",
        "accountDataAgeSeconds": "2.5",
        "accountDataReliable": True,
        "account": account,
    }


def normalize(raw=None, *, policy="BROKER_ALREADY_DEDUCTED", reserved="1000"):
    adapter = MatriksAccountContextAdapter(
        reservation_handling=policy,
        max_account_data_age_seconds=Decimal("60"),
    )
    context = adapter.normalize(
        raw_account=raw or account_payload(),
        raw_positions=[],
        raw_open_orders=[],
        backend_reserved_cash_tl=Decimal(reserved),
        symbol="THYAO",
        market_prices={"THYAO": Decimal("100")},
    )
    assert adapter.last_normalized is not None
    return context, adapter.last_normalized


def test_verified_buying_power_and_decimal_precision_are_normalized():
    context, audit = normalize()

    assert context.account_equity_tl == Decimal("100000.1234567890")
    assert context.effective_available_cash_tl == Decimal("50000.9876543210")
    assert audit.source_fields["broker_reported_buying_power_tl"] == (
        "raw.account.OrderableCash"
    )
    assert audit.account_data_reliable is True


def test_backend_reservation_is_deducted_exactly_once_by_policy():
    context, _ = normalize(policy="BACKEND_DEDUCTED", reserved="1000.25")
    assert context.effective_available_cash_tl == Decimal("49000.7376543210")

    broker_context, _ = normalize(policy="BROKER_ALREADY_DEDUCTED", reserved="1000.25")
    assert broker_context.effective_available_cash_tl == Decimal("50000.9876543210")


def test_t1_t2_and_credit_are_never_promoted_to_cash():
    raw = account_payload()
    raw["account"].pop("OrderableCash")
    context, audit = normalize(raw)

    assert context.effective_available_cash_tl is None
    assert audit.unsettled_receivables_tl == Decimal("9000.00")
    assert audit.credit_limit_tl == Decimal("25000.00")
    assert audit.account_data_reliable is False


def test_margin_buying_power_is_disabled_by_default():
    raw = account_payload(MarginBuyingPower="75000")
    raw["account"].pop("OrderableCash")
    context, audit = normalize(raw)
    assert context.effective_available_cash_tl is None
    assert audit.margin_buying_enabled is False


def test_verified_margin_buying_power_requires_explicit_effective_permission():
    raw = account_payload(MarginBuyingPower="75000")
    raw["account"].pop("OrderableCash")
    adapter = MatriksAccountContextAdapter(
        reservation_handling="BROKER_ALREADY_DEDUCTED",
        allow_margin_buying=True,
    )
    context = adapter.normalize(
        raw_account=raw,
        raw_positions=[],
        raw_open_orders=[],
        backend_reserved_cash_tl=Decimal("0"),
        symbol="THYAO",
        market_prices={"THYAO": Decimal("100")},
    )
    assert context.effective_available_cash_tl == Decimal("75000")
    assert context.account_data_reliable is True


def test_unknown_broker_and_unknown_reservation_policy_fail_closed():
    raw = account_payload()
    raw["sourceProvider"] = "UNVERIFIED_BROKER"
    context, audit = normalize(raw)
    assert context.account_equity_tl is None
    assert context.account_data_reliable is False
    assert "unknown broker/provider mapping" in audit.unreliable_reasons

    unknown_context, unknown_audit = normalize(account_payload(), policy="UNKNOWN")
    assert unknown_context.effective_available_cash_tl is None
    assert unknown_audit.account_data_reliable is False


def test_missing_values_are_not_fabricated_as_zero():
    raw = account_payload()
    raw["account"].pop("TotalEquity")
    raw["account"].pop("OrderableCash")
    context, _ = normalize(raw)
    assert context.account_equity_tl is None
    assert context.effective_available_cash_tl is None


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_money_is_rejected(bad):
    context, audit = normalize(account_payload(OrderableCash=bad))
    assert context.effective_available_cash_tl is None
    assert audit.account_data_reliable is False


def test_stale_and_negative_account_data_fail_closed():
    stale = account_payload()
    stale["accountDataAgeSeconds"] = "61"
    context, audit = normalize(stale)
    assert context.account_data_reliable is False
    assert "account data is stale" in audit.unreliable_reasons

    context, audit = normalize(account_payload(OrderableCash="-1"))
    assert context.effective_available_cash_tl == Decimal("-1")
    assert context.account_data_reliable is False


def test_position_exposure_uses_fresh_prices_and_missing_price_blocks_buy():
    adapter = MatriksAccountContextAdapter(reservation_handling="BACKEND_DEDUCTED")
    positions = [
        {"symbol": "THYAO", "accountNetQty": 10},
        {"symbol": "AKBNK", "accountNetQty": 5},
    ]
    context = adapter.normalize(
        raw_account=account_payload(),
        raw_positions=positions,
        raw_open_orders=[],
        backend_reserved_cash_tl=Decimal("0"),
        symbol="THYAO",
        market_prices={"THYAO": Decimal("100"), "AKBNK": Decimal("50")},
    )
    assert context.current_symbol_value_tl == Decimal("1000")
    assert context.total_account_exposure_tl == Decimal("1250")

    context = adapter.normalize(
        raw_account=account_payload(),
        raw_positions=positions,
        raw_open_orders=[],
        backend_reserved_cash_tl=Decimal("0"),
        symbol="THYAO",
        market_prices={"THYAO": Decimal("100")},
    )
    assert context.total_account_exposure_tl is None
    assert context.account_data_reliable is False
