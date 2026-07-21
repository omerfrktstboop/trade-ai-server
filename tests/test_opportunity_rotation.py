from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import (
    AiDecision,
    BotPosition,
    MarketSnapshot,
    OrderLog,
    PositionLifecycle,
    RotationPlan,
    SystemConfig,
)
from app.models.signal import OrderType, SignalAction, SignalResponse
from app.services.evaluation.pipeline import EvaluationResult
from app.services.effective_risk_config import (
    EffectiveRiskConfigResolver,
    EnvironmentRiskLimits,
    SystemRiskConfig,
)
from app.services.opportunity_rotation import (
    advance_rotation_plan,
    maybe_create_rotation_plan,
)
from app.services.position_sizing import AccountSizingContext, TradeSizingContext
from app.services.trade_profile import get_static_default_profile


@pytest.fixture(autouse=True)
async def _db():
    await drop_all()
    await init_db()
    yield
    await drop_all()
    await init_db()


class RotationGateway:
    def __init__(self, *, mixed: bool = False) -> None:
        total = 5 if mixed else 3
        self.generation = 1
        self.positions = [
            {
                "symbol": "AKBNK",
                "botQty": 3,
                "totalQty": total,
                "sellableQty": total,
                "lockedLongTermQty": 0,
            }
        ]

    async def get_positions(self):
        return {
            "ok": True,
            "positionsLoaded": True,
            "snapshotCompleteFlag": True,
            "accountRef": "f" * 64,
            "confidence": "HIGH",
            "snapshotAgeSeconds": 1,
            "snapshotGeneration": self.generation,
            "positions": self.positions,
        }

    async def get_snapshot(self, symbol: str):
        return {"ok": True, "payload": {"lastPrice": 100, "symbol": symbol}}

    async def get_account(self):
        return {
            "ok": True,
            "accountDataReliable": True,
            "accountDataAgeSeconds": 0,
            "receivedAtUtc": datetime.now(timezone.utc).isoformat(),
            "accountRef": "f" * 64,
            "account": {"Overall": 100000, "AvailableMargin": 50000},
        }


def _projection_inputs():
    limits = EffectiveRiskConfigResolver().resolve(
        environment_limits=EnvironmentRiskLimits(),
        system_config=SystemRiskConfig(total_bot_capital_budget_tl="300000"),
        trade_profile=get_static_default_profile(),
    ).model_copy(
        update={
            "max_qty_per_order": 1000,
            "max_order_value_tl": Decimal("10000"),
            "max_position_value_per_symbol": Decimal("300000"),
            "max_account_exposure_pct": Decimal("100"),
            "minimum_buy_confidence": Decimal("70"),
        }
    )
    account = AccountSizingContext(
        account_equity_tl="500000",
        effective_available_cash_tl="1000",
        reserved_cash_tl="0",
        current_symbol_qty=0,
        current_symbol_value_tl="0",
        total_account_exposure_tl="300000",
        current_bot_symbol_value_tl="0",
        total_bot_exposure_tl="300000",
        account_data_age_seconds="1",
        account_data_reliable=True,
    )
    trade = TradeSizingContext(
        symbol="THYAO",
        entry_price="100",
        stop_loss="95",
        target_price="120",
        confidence="90",
        current_price="100",
        target_allocation_pct="50",
    )
    return limits, account, trade


def _candidate(request_id: str = "target-current") -> EvaluationResult:
    limits, account, trade = _projection_inputs()
    return EvaluationResult(
        response=SignalResponse(
            requestId=request_id,
            symbol="THYAO",
            action=SignalAction.WAIT,
            qty=0,
            orderType=OrderType.NONE,
            confidenceScore=90,
            riskScore=10,
            allowOrder=False,
            reason="budget is full",
        ),
        dispatch_eligible=True,
        decision_created_utc=datetime.now(timezone.utc),
        decision_source="llm",
        raw_action=SignalAction.BUY,
        opportunity_score=90,
        target_allocation_pct=50,
        decision_entry_price=Decimal("100"),
        decision_target_price=Decimal("120"),
        sizing_binding_limits=("bot_budget",),
        sizing_account=account,
        sizing_trade=trade,
        effective_limits=limits,
        rotation_eligible=True,
    )


def _buy_result(request_id: str = "rotation-target-buy") -> EvaluationResult:
    limits, account, trade = _projection_inputs()
    return EvaluationResult(
        response=SignalResponse(
            requestId=request_id,
            symbol="THYAO",
            action=SignalAction.BUY,
            qty=20,
            orderType=OrderType.LIMIT,
            price=Decimal("100"),
            confidenceScore=90,
            riskScore=10,
            allowOrder=True,
            reason="target remains superior",
            targetAllocationPct=Decimal("50"),
            entryRange={"min": 99, "max": 100},
            stopLoss=95,
            targetPrice=120,
        ),
        dispatch_eligible=True,
        decision_created_utc=datetime.now(timezone.utc),
        decision_source="llm",
        raw_action=SignalAction.BUY,
        opportunity_score=90,
        target_allocation_pct=50,
        decision_entry_price=Decimal("100"),
        decision_target_price=Decimal("120"),
        sizing_account=account,
        sizing_trade=trade,
        effective_limits=limits,
        rotation_eligible=True,
    )


async def _seed_rotation_inputs() -> None:
    now = datetime.now(timezone.utc)
    async with async_session_factory() as session:
        session.add_all(
            [
                SystemConfig(
                    key="portfolioRotationEnabled",
                    value="true",
                    value_type="bool",
                ),
                SystemConfig(
                    key="sizingTotalBotCapitalBudgetTl",
                    value="300000",
                    value_type="decimal",
                ),
                SystemConfig(
                    key="sizingMaxQtyPerOrder",
                    value="1000",
                    value_type="int",
                ),
                SystemConfig(
                    key="sizingMaxOrderValueTl",
                    value="10000",
                    value_type="decimal",
                ),
                BotPosition(symbol="AKBNK", qty=3, avg_price=90, total_value=270),
                PositionLifecycle(
                    symbol="AKBNK",
                    status="OPEN",
                    opened_at=now - timedelta(hours=2),
                    current_qty=Decimal("3"),
                    average_entry_price=Decimal("90"),
                    entry_request_id="source-entry",
                    data_quality="VERIFIED",
                    is_backfilled=False,
                ),
                OrderLog(
                    request_id="source-entry",
                    request_fingerprint="a" * 64,
                    account_ref="f" * 64,
                    symbol="AKBNK",
                    action="BUY",
                    qty=3,
                    order_qty=3,
                    filled_qty=3,
                    avg_price=90,
                    limit_price=90,
                    status="FILLED",
                    state="FILLED",
                    created_at=now - timedelta(days=1),
                ),
                AiDecision(
                    request_id="target-previous",
                    symbol="THYAO",
                    provider="deepseek",
                    raw_response={
                        "action": "BUY",
                        "opportunity_score": 88,
                        "target_price": 120,
                    },
                    action="BUY",
                    confidence=88,
                    created_at=now - timedelta(minutes=15),
                ),
                MarketSnapshot(
                    request_id="target-previous",
                    symbol="THYAO",
                    timeframe="Min5",
                    open=99,
                    high=101,
                    low=98,
                    close=100,
                    volume=1000,
                    created_at=now - timedelta(minutes=15),
                ),
                AiDecision(
                    request_id="source-current",
                    symbol="AKBNK",
                    provider="deepseek",
                    raw_response={
                        "action": "WAIT",
                        "opportunity_score": 50,
                        "target_price": 104,
                    },
                    action="WAIT",
                    confidence=70,
                    created_at=now - timedelta(minutes=1),
                ),
                MarketSnapshot(
                    request_id="source-current",
                    symbol="AKBNK",
                    timeframe="Min5",
                    open=99,
                    high=101,
                    low=98,
                    close=100,
                    volume=1000,
                    created_at=now - timedelta(minutes=1),
                ),
            ]
        )
        await session.commit()


async def _plan() -> RotationPlan:
    async with async_session_factory() as session:
        return (await session.execute(select(RotationPlan))).scalar_one()


async def test_plan_requires_strict_bot_only_source_and_two_target_reviews():
    await _seed_rotation_inputs()
    plan = await maybe_create_rotation_plan(
        [_candidate()], gateway=RotationGateway(), account_ref="f" * 64
    )
    assert plan is not None
    assert plan.state == "PLANNED"
    assert plan.source_symbol == "AKBNK"
    assert plan.target_symbol == "THYAO"
    assert plan.source_qty == 3


async def test_mixed_manual_and_bot_position_cannot_be_rotated():
    await _seed_rotation_inputs()
    plan = await maybe_create_rotation_plan(
        [_candidate()], gateway=RotationGateway(mixed=True), account_ref="f" * 64
    )
    assert plan is None


async def test_rotation_advances_one_confirmed_phase_per_tick():
    await _seed_rotation_inputs()
    gateway = RotationGateway()
    created = await maybe_create_rotation_plan(
        [_candidate()], gateway=gateway, account_ref="f" * 64
    )
    assert created is not None
    async with async_session_factory() as session:
        row = await session.get(RotationPlan, created.id)
        row.not_before = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()

    dispatched: list[EvaluationResult] = []
    states_at_dispatch: list[tuple[SignalAction, str]] = []
    evaluation_count = 0

    async def evaluate(_symbol: str):
        nonlocal evaluation_count
        evaluation_count += 1
        return _candidate("target-presale") if evaluation_count == 1 else _buy_result()

    async def dispatch(result: EvaluationResult):
        dispatched.append(result)
        states_at_dispatch.append((result.response.action, (await _plan()).state))
        async with async_session_factory() as session:
            response = result.response
            session.add(
                OrderLog(
                    request_id=response.request_id,
                    request_fingerprint="a" * 64,
                    account_ref="f" * 64,
                    symbol=response.symbol,
                    action=response.action.value,
                    qty=response.qty,
                    order_qty=response.qty,
                    filled_qty=0,
                    price=float(response.price),
                    limit_price=float(response.price),
                    status="SENT_PENDING",
                    state="SENT_PENDING",
                )
            )
            await session.commit()

    await advance_rotation_plan(
        gateway=gateway,
        account_ref="f" * 64,
        evaluate=evaluate,
        dispatch=dispatch,
    )
    assert (await _plan()).state == "SELL_PENDING"
    assert [item.response.action for item in dispatched] == [SignalAction.SELL]
    assert states_at_dispatch == [(SignalAction.SELL, "SELL_PENDING")]

    async with async_session_factory() as session:
        sell = (
            await session.execute(
                select(OrderLog).where(OrderLog.action == "SELL")
            )
        ).scalar_one()
        sell.status = sell.state = "FILLED"
        sell.filled_qty = sell.order_qty
        await session.commit()

    await advance_rotation_plan(
        gateway=gateway,
        account_ref="f" * 64,
        evaluate=evaluate,
        dispatch=dispatch,
    )
    assert (await _plan()).state == "SELL_FILLED_WAIT_REFRESH"
    assert len(dispatched) == 1

    gateway.generation = 2
    gateway.positions = []
    await advance_rotation_plan(
        gateway=gateway,
        account_ref="f" * 64,
        evaluate=evaluate,
        dispatch=dispatch,
    )
    assert (await _plan()).state == "CASH_CONFIRMED"
    assert len(dispatched) == 1

    await advance_rotation_plan(
        gateway=gateway,
        account_ref="f" * 64,
        evaluate=evaluate,
        dispatch=dispatch,
    )
    assert (await _plan()).state == "BUY_PENDING"
    assert [item.response.action for item in dispatched] == [
        SignalAction.SELL,
        SignalAction.BUY,
    ]
    assert states_at_dispatch[-1] == (SignalAction.BUY, "BUY_PENDING")

    async with async_session_factory() as session:
        buy = (
            await session.execute(
                select(OrderLog).where(
                    OrderLog.action == "BUY", OrderLog.symbol == "THYAO"
                )
            )
        ).scalar_one()
        buy.status = buy.state = "FILLED"
        buy.filled_qty = buy.order_qty
        await session.commit()

    await advance_rotation_plan(
        gateway=gateway,
        account_ref="f" * 64,
        evaluate=evaluate,
        dispatch=dispatch,
    )
    assert (await _plan()).state == "COMPLETED"


async def test_expired_uncertain_sell_stays_active_until_reconciled():
    await _seed_rotation_inputs()
    gateway = RotationGateway()
    created = await maybe_create_rotation_plan(
        [_candidate()], gateway=gateway, account_ref="f" * 64
    )
    assert created is not None
    async with async_session_factory() as session:
        row = await session.get(RotationPlan, created.id)
        row.state = "SELL_PENDING"
        row.sell_request_id = f"rotation-{created.id}-sell"
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(
            OrderLog(
                request_id=row.sell_request_id,
                request_fingerprint="c" * 64,
                account_ref="f" * 64,
                symbol="AKBNK",
                action="SELL",
                qty=3,
                order_qty=3,
                filled_qty=0,
                limit_price=100,
                status="SEND_UNKNOWN",
                state="SEND_UNKNOWN",
            )
        )
        await session.commit()

    async def no_evaluate(_symbol: str):
        raise AssertionError("target must not be evaluated")

    async def no_dispatch(_result: EvaluationResult):
        raise AssertionError("uncertain order must not be resent")

    assert await advance_rotation_plan(
        gateway=gateway,
        account_ref="f" * 64,
        evaluate=no_evaluate,
        dispatch=no_dispatch,
    )
    assert (await _plan()).state == "SELL_PENDING"


async def test_disabled_pending_buy_stays_active_until_reconciled():
    await _seed_rotation_inputs()
    gateway = RotationGateway()
    created = await maybe_create_rotation_plan(
        [_candidate()], gateway=gateway, account_ref="f" * 64
    )
    assert created is not None
    async with async_session_factory() as session:
        row = await session.get(RotationPlan, created.id)
        row.state = "BUY_PENDING"
        row.buy_request_id = "rotation-pending-buy"
        row.target_qty = 4
        enabled = (
            await session.execute(
                select(SystemConfig).where(
                    SystemConfig.key == "portfolioRotationEnabled"
                )
            )
        ).scalar_one()
        assert enabled is not None
        enabled.value = "false"
        session.add(
            OrderLog(
                request_id=row.buy_request_id,
                request_fingerprint="d" * 64,
                account_ref="f" * 64,
                symbol="THYAO",
                action="BUY",
                qty=4,
                order_qty=4,
                filled_qty=0,
                limit_price=100,
                status="SENT_PENDING",
                state="SENT_PENDING",
            )
        )
        await session.commit()

    async def no_evaluate(_symbol: str):
        raise AssertionError("target must not be evaluated")

    async def no_dispatch(_result: EvaluationResult):
        raise AssertionError("pending order must not be resent")

    assert await advance_rotation_plan(
        gateway=gateway,
        account_ref="f" * 64,
        evaluate=no_evaluate,
        dispatch=no_dispatch,
    )
    assert (await _plan()).state == "BUY_PENDING"
