"""Targeted tests for Task 1-4: the real fill ledger, position lifecycle P&L,
decision outcome tracking / labeler, and the fill-linked stop-loss guard.

Mirrors the fixture/session style already used in tests/test_order_result.py
and tests/test_stop_loss_guard.py (drop_all/init_db per test, direct
async_session_factory usage, FakeGateway for gateway-backed calls).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import (
    BotPosition,
    DecisionOutcome,
    OrderFill,
    PositionLifecycle,
    PositionStopEvent,
    RiskDecision,
)
from app.models.signal import SignalAction, SignalMode, SignalRequest, SignalResponse
from app.services.admin_config import get_fee_config, set_admin_config_value
from app.services.fill_ledger import compute_fill_costs, compute_slippage, to_decimal
from app.services.matriks_gateway import MatriksGatewayClient
from app.services.order_lifecycle import apply_callback
from app.services.outcome_labeler import label_pending_outcomes
from app.services.outcome_tracking import create_decision_outcome
from app.services.position_lifecycle_engine import get_open_lifecycle
from app.services.stop_loss_guard import check_stop_loss_positions
from tests.fake_gateway import FakeGateway


def make_gateway_client(fake: FakeGateway) -> MatriksGatewayClient:
    return MatriksGatewayClient(
        base_url="http://fake-gateway", token=fake.token, transport=fake.transport
    )


@pytest.fixture(autouse=True)
def _reset_db():
    asyncio.run(drop_all())
    asyncio.run(init_db())
    yield
    asyncio.run(drop_all())
    asyncio.run(init_db())


async def _seed_decision(
    request_id: str,
    symbol: str,
    *,
    stop_loss: float | None = 90.0,
    target_price: float | None = 120.0,
    action: str = "BUY",
    allow_order: bool = True,
) -> None:
    async with async_session_factory() as session:
        session.add(
            RiskDecision(
                request_id=request_id,
                symbol=symbol,
                action=action,
                confidence=90.0,
                risk_score=10.0,
                allow_order=allow_order,
                stop_loss=stop_loss,
                target_price=target_price,
                order_type="LIMIT",
                qty=10,
                mode="DEMO_LIVE",
            )
        )
        await session.commit()


async def _fill(
    request_id: str,
    symbol: str,
    action: str,
    *,
    order_qty: float,
    filled_qty: float,
    last_fill_qty: float,
    avg_price: float | None,
    limit_price: float | None = 100.0,
    status: str = "PARTIALLY_FILLED",
    order_id: str = "ord-1",
):
    async with async_session_factory() as session:
        row, changed = await apply_callback(
            session,
            request_id=request_id,
            symbol=symbol,
            action=action,
            status=status,
            order_qty=order_qty,
            filled_qty=filled_qty,
            last_fill_qty=last_fill_qty,
            avg_price=avg_price,
            limit_price=limit_price,
            order_id=order_id,
            message="test fill",
        )
        return row, changed


async def _fills_for(request_id: str) -> list[OrderFill]:
    async with async_session_factory() as session:
        return list(
            (
                await session.execute(
                    select(OrderFill)
                    .where(OrderFill.request_id == request_id)
                    .order_by(OrderFill.id.asc())
                )
            )
            .scalars()
            .all()
        )


async def _lifecycle(symbol: str) -> PositionLifecycle | None:
    async with async_session_factory() as session:
        return await get_open_lifecycle(session, symbol)


async def _stop_events(lifecycle_id: int) -> list[PositionStopEvent]:
    async with async_session_factory() as session:
        return list(
            (
                await session.execute(
                    select(PositionStopEvent)
                    .where(PositionStopEvent.position_lifecycle_id == lifecycle_id)
                    .order_by(PositionStopEvent.id.asc())
                )
            )
            .scalars()
            .all()
        )


# ── Fill ledger (Task 1.1 / 1.2 / 1.4) ─────────────────────────────────────


class TestFillLedger:
    async def test_first_fill_creates_order_fill_with_full_qty_and_price(self):
        await _seed_decision("req-1", "THYAO")
        await _fill(
            "req-1", "THYAO", "BUY",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=100.0, limit_price=100.0, status="FILLED",
        )
        fills = await _fills_for("req-1")
        assert len(fills) == 1
        assert fills[0].fill_qty == Decimal("10")
        assert fills[0].fill_price == Decimal("100")

    async def test_two_partial_fills_produce_two_delta_records(self):
        await _seed_decision("req-2", "THYAO")
        await _fill(
            "req-2", "THYAO", "BUY",
            order_qty=10, filled_qty=4, last_fill_qty=4,
            avg_price=100.0, status="PARTIALLY_FILLED",
        )
        await _fill(
            "req-2", "THYAO", "BUY",
            order_qty=10, filled_qty=10, last_fill_qty=6,
            avg_price=103.0, status="FILLED",
        )
        fills = await _fills_for("req-2")
        assert len(fills) == 2
        assert fills[0].fill_qty == Decimal("4")
        assert fills[0].fill_price == Decimal("100")
        # derived delta price = (103*10 - 100*4) / 6 = 105
        assert fills[1].fill_qty == Decimal("6")
        assert fills[1].fill_price == Decimal("105")

    async def test_duplicate_callback_does_not_double_record(self):
        await _seed_decision("req-3", "THYAO")
        await _fill(
            "req-3", "THYAO", "BUY",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=100.0, status="FILLED",
        )
        # Gateway retries the exact same final callback.
        await _fill(
            "req-3", "THYAO", "BUY",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=100.0, status="FILLED",
        )
        fills = await _fills_for("req-3")
        assert len(fills) == 1

    async def test_stale_lower_cumulative_qty_creates_no_fill(self):
        await _seed_decision("req-4", "THYAO")
        await _fill(
            "req-4", "THYAO", "BUY",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=100.0, status="FILLED",
        )
        # A stale, lower cumulative qty arrives late.
        await _fill(
            "req-4", "THYAO", "BUY",
            order_qty=10, filled_qty=5, last_fill_qty=5,
            avg_price=100.0, status="PARTIALLY_FILLED",
        )
        fills = await _fills_for("req-4")
        assert len(fills) == 1
        assert fills[0].fill_qty == Decimal("10")

    async def test_rejected_order_produces_no_fill(self):
        await _seed_decision("req-5", "THYAO")
        await _fill(
            "req-5", "THYAO", "BUY",
            order_qty=10, filled_qty=0, last_fill_qty=0,
            avg_price=None, status="REJECTED",
        )
        assert await _fills_for("req-5") == []
        assert await _lifecycle("THYAO") is None

    async def test_missing_avg_price_produces_no_fill(self):
        await _seed_decision("req-6", "THYAO")
        await _fill(
            "req-6", "THYAO", "BUY",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=None, status="FILLED",
        )
        assert await _fills_for("req-6") == []

    async def test_commission_uses_configured_rate_with_minimum(self):
        async with async_session_factory() as session:
            await set_admin_config_value(
                session, "commissionBps", "10", changed_by="test"
            )
            await set_admin_config_value(
                session, "minimumCommissionTl", "5", changed_by="test"
            )
            fee_config = await get_fee_config(session)
        commission, _, _, total = compute_fill_costs(fee_config, Decimal("1000"))
        # 1000 * 10bps = 1.0, below the 5 TL minimum -> minimum applies.
        assert commission == Decimal("5")
        assert total == Decimal("5")

    async def test_zero_config_produces_zero_cost(self):
        async with async_session_factory() as session:
            fee_config = await get_fee_config(session)
        commission, exchange, other, total = compute_fill_costs(
            fee_config, Decimal("5000")
        )
        assert commission == exchange == other == total == Decimal("0")

    def test_slippage_none_without_limit_price(self):
        slippage_tl, slippage_pct = compute_slippage("BUY", Decimal("100"), None)
        assert slippage_tl is None
        assert slippage_pct is None

    def test_buy_slippage_sign(self):
        slippage_tl, _ = compute_slippage("BUY", Decimal("101"), Decimal("100"))
        assert slippage_tl == Decimal("1")

    def test_sell_slippage_sign(self):
        slippage_tl, _ = compute_slippage("SELL", Decimal("99"), Decimal("100"))
        assert slippage_tl == Decimal("1")


# ── Position lifecycle & realized P&L (Task 1.3) ───────────────────────────


class TestPositionLifecycle:
    async def test_first_buy_fill_opens_lifecycle(self):
        await _seed_decision("req-10", "THYAO", stop_loss=90.0, target_price=120.0)
        await _fill(
            "req-10", "THYAO", "BUY",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=100.0, status="FILLED",
        )
        lc = await _lifecycle("THYAO")
        assert lc is not None
        assert lc.status == "OPEN"
        assert lc.current_qty == Decimal("10")
        assert lc.average_entry_price == Decimal("100")
        assert lc.initial_stop_loss == Decimal("90")
        assert lc.active_target_price == Decimal("120")

    async def test_second_buy_updates_weighted_average_cost(self):
        await _seed_decision("req-11a", "THYAO", stop_loss=90.0)
        await _fill(
            "req-11a", "THYAO", "BUY",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=100.0, status="FILLED",
        )
        await _seed_decision("req-11b", "THYAO", stop_loss=90.0)
        await _fill(
            "req-11b", "THYAO", "BUY",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=110.0, status="FILLED", order_id="ord-2",
        )
        lc = await _lifecycle("THYAO")
        assert lc.current_qty == Decimal("20")
        assert lc.average_entry_price == Decimal("105")

    async def test_partial_sell_keeps_lifecycle_open_with_proportional_pnl(self):
        await _seed_decision("req-12", "THYAO", stop_loss=90.0)
        await _fill(
            "req-12", "THYAO", "BUY",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=100.0, status="FILLED",
        )
        await _seed_decision("req-12-sell", "THYAO", action="SELL", stop_loss=None)
        await _fill(
            "req-12-sell", "THYAO", "SELL",
            order_qty=4, filled_qty=4, last_fill_qty=4,
            avg_price=110.0, status="FILLED", order_id="ord-2",
        )
        lc = await _lifecycle("THYAO")
        assert lc.status == "OPEN"
        assert lc.current_qty == Decimal("6")
        # gross = 4 * (110 - 100) = 40
        assert lc.gross_realized_pnl_tl == Decimal("40")

    async def test_full_sell_closes_lifecycle(self):
        await _seed_decision("req-13", "THYAO", stop_loss=90.0)
        await _fill(
            "req-13", "THYAO", "BUY",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=100.0, status="FILLED",
        )
        await _seed_decision("req-13-sell", "THYAO", action="SELL", stop_loss=None)
        await _fill(
            "req-13-sell", "THYAO", "SELL",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=110.0, status="FILLED", order_id="ord-2",
        )
        assert await _lifecycle("THYAO") is None
        async with async_session_factory() as session:
            closed = (
                await session.execute(
                    select(PositionLifecycle).where(PositionLifecycle.symbol == "THYAO")
                )
            ).scalar_one()
        assert closed.status == "CLOSED"
        assert closed.current_qty == Decimal("0")
        assert closed.gross_realized_pnl_tl == Decimal("100")  # 10 * (110-100)

    async def test_new_position_after_close_opens_fresh_lifecycle(self):
        await _seed_decision("req-14a", "THYAO", stop_loss=90.0)
        await _fill(
            "req-14a", "THYAO", "BUY",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=100.0, status="FILLED",
        )
        await _seed_decision("req-14b", "THYAO", action="SELL", stop_loss=None)
        await _fill(
            "req-14b", "THYAO", "SELL",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=110.0, status="FILLED", order_id="ord-2",
        )
        await _seed_decision("req-14c", "THYAO", stop_loss=95.0)
        await _fill(
            "req-14c", "THYAO", "BUY",
            order_qty=5, filled_qty=5, last_fill_qty=5,
            avg_price=120.0, status="FILLED", order_id="ord-3",
        )
        lc = await _lifecycle("THYAO")
        assert lc is not None
        assert lc.current_qty == Decimal("5")
        assert lc.average_entry_price == Decimal("120")
        async with async_session_factory() as session:
            all_lifecycles = (
                (
                    await session.execute(
                        select(PositionLifecycle).where(PositionLifecycle.symbol == "THYAO")
                    )
                )
                .scalars()
                .all()
            )
        assert len(all_lifecycles) == 2

    async def test_net_pnl_deducts_buy_and_sell_costs(self):
        async with async_session_factory() as session:
            await set_admin_config_value(
                session, "commissionBps", "10", changed_by="test"
            )
        await _seed_decision("req-15", "THYAO", stop_loss=90.0)
        await _fill(
            "req-15", "THYAO", "BUY",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=100.0, status="FILLED",
        )
        await _seed_decision("req-15-sell", "THYAO", action="SELL", stop_loss=None)
        await _fill(
            "req-15-sell", "THYAO", "SELL",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=110.0, status="FILLED", order_id="ord-2",
        )
        async with async_session_factory() as session:
            closed = (
                await session.execute(
                    select(PositionLifecycle).where(PositionLifecycle.symbol == "THYAO")
                )
            ).scalar_one()
        assert closed.net_realized_pnl_tl < closed.gross_realized_pnl_tl
        assert closed.total_buy_cost_tl > Decimal("0")
        assert closed.total_sell_cost_tl > Decimal("0")

    async def test_sell_with_no_open_lifecycle_does_not_crash_or_fabricate(self):
        await _seed_decision("req-16", "THYAO", action="SELL", stop_loss=None)
        row, changed = await _fill(
            "req-16", "THYAO", "SELL",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=110.0, status="FILLED",
        )
        assert row.status == "FILLED"
        assert await _lifecycle("THYAO") is None

    async def test_pnl_uses_fill_price_not_order_limit_price(self):
        await _seed_decision("req-17", "THYAO", stop_loss=90.0)
        # limit_price=100 but the real fill executed at 98 (better than limit).
        await _fill(
            "req-17", "THYAO", "BUY",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=98.0, limit_price=100.0, status="FILLED",
        )
        lc = await _lifecycle("THYAO")
        assert lc.average_entry_price == Decimal("98")


# ── Decision outcome creation + labeler (Task 3) ───────────────────────────


class TestDecisionOutcome:
    async def _sig_request(self, request_id: str, symbol: str = "THYAO") -> SignalRequest:
        return SignalRequest(
            requestId=request_id,
            symbol=symbol,
            timeframe="MIN5",
            lastPrice=100.0,
            open=99.0,
            high=101.0,
            low=98.0,
            close=100.0,
            volume=1000.0,
            mode=SignalMode.PAPER,
        )

    def _response(self, action: SignalAction = SignalAction.BUY) -> SignalResponse:
        return SignalResponse(
            requestId="req-20",
            symbol="THYAO",
            action=action,
            qty=10 if action != SignalAction.WAIT else 0,
            orderType="LIMIT" if action != SignalAction.WAIT else "NONE",
            price=to_decimal(100.0) if action != SignalAction.WAIT else None,
            confidenceScore=80.0,
            riskScore=20.0,
            allowOrder=action != SignalAction.WAIT,
            requiresConfirmation=False,
            reason="test",
            entryRange=None,
            stopLoss=to_decimal(90.0) if action == SignalAction.BUY else None,
            targetPrice=to_decimal(120.0) if action == SignalAction.BUY else None,
        )

    async def test_creates_one_pending_row_and_is_idempotent(self):
        req = await self._sig_request("req-20")
        response = self._response()
        async with async_session_factory() as session:
            await create_decision_outcome(session, req, {}, {}, response)
            await create_decision_outcome(session, req, {}, {}, response)
            await session.commit()
            rows = (
                (
                    await session.execute(
                        select(DecisionOutcome).where(
                            DecisionOutcome.request_id == "req-20"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].outcome_status == "PENDING"

    async def test_wait_decision_also_gets_an_outcome_row(self):
        req = await self._sig_request("req-21")
        response = self._response(SignalAction.WAIT)
        async with async_session_factory() as session:
            await create_decision_outcome(session, req, {}, {}, response)
            await session.commit()
            row = (
                await session.execute(
                    select(DecisionOutcome).where(DecisionOutcome.request_id == "req-21")
                )
            ).scalar_one()
        assert row.decision_action == "WAIT"


class TestOutcomeLabeler:
    async def _seed_outcome(
        self,
        request_id: str,
        *,
        decision_price: float = 100.0,
        stop_loss: float | None = 90.0,
        target_price: float | None = 120.0,
        decision_action: str = "BUY",
        minutes_ago: int = 10,
    ) -> None:
        async with async_session_factory() as session:
            session.add(
                DecisionOutcome(
                    request_id=request_id,
                    symbol="THYAO",
                    evaluation_purpose="TRADING",
                    decision_action=decision_action,
                    decision_price=Decimal(str(decision_price)),
                    decision_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
                    stop_loss=Decimal(str(stop_loss)) if stop_loss else None,
                    target_price=Decimal(str(target_price)) if target_price else None,
                    outcome_status="PENDING",
                )
            )
            await session.commit()

    async def _get_outcome(self, request_id: str) -> DecisionOutcome:
        async with async_session_factory() as session:
            return (
                await session.execute(
                    select(DecisionOutcome).where(DecisionOutcome.request_id == request_id)
                )
            ).scalar_one()

    async def test_due_horizon_filled_with_reliable_price(self):
        await self._seed_outcome("out-1", minutes_ago=10)
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {"lastPrice": 105.0, "quoteReliable": True}
        await label_pending_outcomes(make_gateway_client(fake))
        outcome = await self._get_outcome("out-1")
        assert outcome.future_return_5m == Decimal("5")

    async def test_horizon_not_due_yet_stays_none(self):
        await self._seed_outcome("out-2", minutes_ago=1)
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {"lastPrice": 105.0, "quoteReliable": True}
        await label_pending_outcomes(make_gateway_client(fake))
        outcome = await self._get_outcome("out-2")
        assert outcome.future_return_5m is None

    async def test_unreliable_price_leaves_fields_untouched(self):
        await self._seed_outcome("out-3", minutes_ago=10)
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {"quoteReliable": False}
        await label_pending_outcomes(make_gateway_client(fake))
        outcome = await self._get_outcome("out-3")
        assert outcome.future_return_5m is None
        assert outcome.outcome_status == "PENDING"

    async def test_mfe_mae_track_running_extrema(self):
        await self._seed_outcome("out-4", minutes_ago=10)
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {"lastPrice": 105.0, "quoteReliable": True}
        await label_pending_outcomes(make_gateway_client(fake))
        first = await self._get_outcome("out-4")
        assert first.mfe_pct == Decimal("5")
        assert first.mae_pct == Decimal("5")
        # Re-run with a worse price - MAE should extend down, MFE stay.
        fake.snapshot_overrides["THYAO"] = {"lastPrice": 95.0, "quoteReliable": True}
        await label_pending_outcomes(make_gateway_client(fake))
        second = await self._get_outcome("out-4")
        assert second.mfe_pct == Decimal("5")
        assert second.mae_pct == Decimal("-5")

    async def test_target_hit_before_stop(self):
        await self._seed_outcome("out-5", minutes_ago=10, stop_loss=90.0, target_price=104.0)
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {"lastPrice": 105.0, "quoteReliable": True}
        await label_pending_outcomes(make_gateway_client(fake))
        outcome = await self._get_outcome("out-5")
        assert outcome.target_hit_before_stop is True
        assert outcome.target_hit_at is not None

    async def test_stop_hit_before_target(self):
        await self._seed_outcome("out-6", minutes_ago=10, stop_loss=96.0, target_price=120.0)
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {"lastPrice": 95.0, "quoteReliable": True}
        await label_pending_outcomes(make_gateway_client(fake))
        outcome = await self._get_outcome("out-6")
        assert outcome.target_hit_before_stop is False
        assert outcome.stop_hit_at is not None

    async def test_simultaneous_target_and_stop_is_ambiguous(self):
        # target below current price AND stop above current price at once -
        # only possible to construct directly, not through normal trading,
        # but the labeler must not guess an order in this degenerate case.
        await self._seed_outcome("out-7", minutes_ago=10, stop_loss=110.0, target_price=90.0)
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {"lastPrice": 100.0, "quoteReliable": True}
        await label_pending_outcomes(make_gateway_client(fake))
        outcome = await self._get_outcome("out-7")
        assert outcome.outcome_status == "AMBIGUOUS"
        assert outcome.target_hit_before_stop is None

    async def test_wait_decision_gets_forward_return_no_target_stop_logic(self):
        await self._seed_outcome(
            "out-8", minutes_ago=10, decision_action="WAIT", stop_loss=None, target_price=None
        )
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {"lastPrice": 105.0, "quoteReliable": True}
        await label_pending_outcomes(make_gateway_client(fake))
        outcome = await self._get_outcome("out-8")
        assert outcome.future_return_5m == Decimal("5")
        assert outcome.target_hit_before_stop is None


# ── Position-linked stop-loss (Task 4) ─────────────────────────────────────


class TestPositionLinkedStop:
    async def _seed_bot_position(self, symbol: str, qty: float) -> None:
        async with async_session_factory() as session:
            session.add(BotPosition(symbol=symbol, qty=qty, avg_price=100.0))
            await session.commit()

    async def test_unfilled_buy_creates_no_lifecycle_or_stop(self):
        await _seed_decision("req-30", "THYAO", stop_loss=90.0)
        # Decision allowed, but the order never fills (still pending).
        await _fill(
            "req-30", "THYAO", "BUY",
            order_qty=10, filled_qty=0, last_fill_qty=0,
            avg_price=None, status="SENT_PENDING",
        )
        assert await _lifecycle("THYAO") is None

    async def test_stop_only_tightens_never_loosens(self):
        await _seed_decision("req-31a", "THYAO", stop_loss=90.0)
        await _fill(
            "req-31a", "THYAO", "BUY",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=100.0, status="FILLED",
        )
        # A second BUY decision with a LOOSER (lower) stop must not weaken it.
        await _seed_decision("req-31b", "THYAO", stop_loss=80.0)
        await _fill(
            "req-31b", "THYAO", "BUY",
            order_qty=5, filled_qty=5, last_fill_qty=5,
            avg_price=100.0, status="FILLED", order_id="ord-2",
        )
        lc = await _lifecycle("THYAO")
        assert lc.active_stop_loss == Decimal("90")  # unchanged, not loosened

        # A third BUY with a TIGHTER (higher) stop does update it.
        await _seed_decision("req-31c", "THYAO", stop_loss=95.0)
        await _fill(
            "req-31c", "THYAO", "BUY",
            order_qty=5, filled_qty=5, last_fill_qty=5,
            avg_price=100.0, status="FILLED", order_id="ord-3",
        )
        lc = await _lifecycle("THYAO")
        assert lc.active_stop_loss == Decimal("95")

    async def test_missing_new_stop_does_not_delete_existing_stop(self):
        await _seed_decision("req-32a", "THYAO", stop_loss=90.0)
        await _fill(
            "req-32a", "THYAO", "BUY",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=100.0, status="FILLED",
        )
        await _seed_decision("req-32b", "THYAO", stop_loss=None)
        await _fill(
            "req-32b", "THYAO", "BUY",
            order_qty=5, filled_qty=5, last_fill_qty=5,
            avg_price=100.0, status="FILLED", order_id="ord-2",
        )
        lc = await _lifecycle("THYAO")
        assert lc.active_stop_loss == Decimal("90")

    async def test_new_target_does_not_silently_override_existing(self):
        await _seed_decision("req-33a", "THYAO", stop_loss=90.0, target_price=120.0)
        await _fill(
            "req-33a", "THYAO", "BUY",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=100.0, status="FILLED",
        )
        await _seed_decision("req-33b", "THYAO", stop_loss=90.0, target_price=130.0)
        await _fill(
            "req-33b", "THYAO", "BUY",
            order_qty=5, filled_qty=5, last_fill_qty=5,
            avg_price=100.0, status="FILLED", order_id="ord-2",
        )
        lc = await _lifecycle("THYAO")
        assert lc.active_target_price == Decimal("120")

    async def test_guard_sells_exact_lifecycle_remaining_qty(self):
        await _seed_decision("req-34", "THYAO", stop_loss=90.0)
        await _fill(
            "req-34", "THYAO", "BUY",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=100.0, status="FILLED",
        )
        await _seed_decision("req-34-sell", "THYAO", action="SELL", stop_loss=None)
        await _fill(
            "req-34-sell", "THYAO", "SELL",
            order_qty=4, filled_qty=4, last_fill_qty=4,
            avg_price=95.0, status="FILLED", order_id="ord-2",
        )
        # BotPosition is the guard's trigger-iteration source (unchanged);
        # only the stop/qty values now come from the lifecycle.
        await self._seed_bot_position("THYAO", 6)
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {"lastPrice": 85.0}
        triggered = await check_stop_loss_positions(make_gateway_client(fake))
        assert len(triggered) == 1
        assert triggered[0].response.qty == 6  # 10 bought - 4 sold

    async def test_closed_lifecycle_stop_never_retriggers(self):
        await _seed_decision("req-35", "THYAO", stop_loss=90.0)
        await _fill(
            "req-35", "THYAO", "BUY",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=100.0, status="FILLED",
        )
        await _seed_decision("req-35-sell", "THYAO", action="SELL", stop_loss=None)
        await _fill(
            "req-35-sell", "THYAO", "SELL",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=110.0, status="FILLED", order_id="ord-2",
        )
        # BotPosition still shows the stale pre-sale qty (sync hasn't run
        # yet) - the guard must still find no OPEN lifecycle and do nothing,
        # rather than trusting BotPosition's qty for the sell size.
        await self._seed_bot_position("THYAO", 10)
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {"lastPrice": 50.0}
        triggered = await check_stop_loss_positions(make_gateway_client(fake))
        assert triggered == []

    async def test_stop_events_audit_trail_created(self):
        await _seed_decision("req-36a", "THYAO", stop_loss=90.0)
        await _fill(
            "req-36a", "THYAO", "BUY",
            order_qty=10, filled_qty=10, last_fill_qty=10,
            avg_price=100.0, status="FILLED",
        )
        lc = await _lifecycle("THYAO")
        events = await _stop_events(lc.id)
        assert any(e.event_type == "INITIAL_STOP_CREATED" for e in events)

        await _seed_decision("req-36b", "THYAO", stop_loss=95.0)
        await _fill(
            "req-36b", "THYAO", "BUY",
            order_qty=5, filled_qty=5, last_fill_qty=5,
            avg_price=100.0, status="FILLED", order_id="ord-2",
        )
        events = await _stop_events(lc.id)
        assert any(e.event_type == "STOP_TIGHTENED" for e in events)

        await _seed_decision("req-36-sell", "THYAO", action="SELL", stop_loss=None)
        await _fill(
            "req-36-sell", "THYAO", "SELL",
            order_qty=15, filled_qty=15, last_fill_qty=15,
            avg_price=110.0, status="FILLED", order_id="ord-3",
        )
        events = await _stop_events(lc.id)
        assert any(e.event_type == "POSITION_CLOSED" for e in events)
