from __future__ import annotations

from decimal import Decimal

from app.core.risk_config import RiskConfig
from app.models.signal import EntryRange, SignalAction, SignalMode, SignalRequest
from app.services.effective_risk_config import EffectiveRiskConfig
from app.services.position_sizing import AccountSizingContext
from app.services.risk_engine import RiskDecision, RiskEngine


def effective(**overrides) -> EffectiveRiskConfig:
    data = {
        "risk_per_trade_pct": "1",
        "max_cash_utilization_pct": "100",
        "max_account_exposure_pct": "100",
        "max_position_value_per_symbol": "5000",
        "max_order_value_tl": "1000",
        "max_qty_per_order": 20,
        "min_order_value_tl": "1",
        "min_stop_distance_pct": "0.1",
        "max_stop_distance_pct": "10",
        "minimum_stop_slippage_pct": "0.1",
        "maximum_stop_slippage_pct": "1",
        "profile_stop_slippage_pct": "0.5",
        "max_account_data_age_seconds": "60",
        "minimum_buy_confidence": "75",
        "minimum_sell_confidence": "70",
        "daily_order_limit": 3,
        "per_symbol_daily_order_limit": 1,
        "sizing_enabled": True,
        "demo_orders_enabled": True,
        "real_orders_enabled": False,
        "environment_config_fingerprint": "f" * 64,
        "system_config_version": "test",
        "trade_profile_id": 1,
        "trade_profile_code": "NORMAL",
        "trade_profile_version": 2,
    }
    data.update(overrides)
    return EffectiveRiskConfig(**data)


def request(*, with_account=True, **updates) -> SignalRequest:
    account = AccountSizingContext(
        account_equity_tl="100000",
        effective_available_cash_tl="50000",
        reserved_cash_tl="0",
        current_symbol_qty=0,
        current_symbol_value_tl="0",
        total_account_exposure_tl="0",
        account_data_age_seconds="1",
        account_data_reliable=True,
    )
    data = {
        "requestId": "task1a-1",
        "symbol": "THYAO",
        "timeframe": "Min5",
        "lastPrice": 100,
        "open": 99,
        "high": 101,
        "low": 98,
        "volume": 1000,
        "mode": SignalMode.DEMO_LIVE,
        "tradeEligible": True,
        "accountSizingContext": account if with_account else None,
    }
    data.update(updates)
    return SignalRequest(**data)


def decision(qty=999999) -> RiskDecision:
    return RiskDecision(
        action=SignalAction.BUY,
        confidence=82,
        qty=qty,
        entry_range=EntryRange(min="99", max="100"),
        stop_loss=Decimal("95"),
        target_price=Decimal("110"),
        reason="AI signal",
    )


def engine(**limit_overrides) -> RiskEngine:
    legacy = RiskConfig(
        allowed_symbols="THYAO",
        max_position_value_per_symbol=100000,
        max_daily_trade_count=100,
        disable_trading_after="23:59",
        timezone="Etc/GMT+12",
        require_alpha_trend_alignment=False,
        require_indicator_consensus_alignment=False,
        _env_file="",
    )
    return RiskEngine(legacy, effective(**limit_overrides))


def test_ai_qty_is_ignored_and_server_qty_is_used():
    risk_engine = engine(max_qty_per_order=7)
    response = risk_engine.evaluate(request(), decision(qty=999999))
    assert response.action == SignalAction.BUY
    assert response.qty == 7
    assert risk_engine.last_sizing_result is not None
    assert "profile_max_qty" in risk_engine.last_sizing_result.binding_limits


def test_account_adapter_absence_blocks_demo_live_buy():
    response = engine().evaluate(request(with_account=False), decision())
    assert response.action == SignalAction.WAIT
    assert not response.allow_order
    assert "TASK 1B" in response.reason


def test_risk_engine_recomputes_notional_and_stop_invariant():
    risk_engine = engine(max_order_value_tl="300", risk_per_trade_pct="0.1")
    response = risk_engine.evaluate(request(), decision(qty=1))
    sizing = risk_engine.last_sizing_result
    assert sizing is not None and sizing.allowed
    assert response.qty == sizing.qty
    assert sizing.order_value_tl <= Decimal("300")
    assert sizing.estimated_loss_at_stop_tl <= sizing.risk_budget_tl


def test_sell_quantity_comes_from_bot_owned_sellable_lots():
    response = engine().evaluate(
        request(
            with_account=False,
            botPositionQty=8,
            totalAccountQty=20,
            accountAvailableQty=20,
            lockedLongTermQty=15,
        ),
        RiskDecision(action=SignalAction.SELL, confidence=90, qty=999),
    )
    assert response.action == SignalAction.SELL
    assert response.qty == 5
