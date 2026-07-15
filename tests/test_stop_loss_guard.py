"""Tests for the deterministic, AI-independent stop-loss guard
(app/services/stop_loss_guard.py).

check_stop_loss_positions() is tested in isolation (no scanner instance
needed - it only detects breaches and returns EvaluationResults). Dispatch
through the existing order gates (kill switch, cooldown) is tested via
SymbolScanner._run_stop_loss_guard, mirroring tests/test_scanner.py's
TestOrderPath patterns.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Mode
from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import BotPosition, RiskDecision, SystemConfig, TradeWatchlistSymbol
from app.models.signal import SignalAction
from app.services import scanner as scanner_module
from app.services import stop_loss_guard as guard_module
from app.services.matriks_gateway import GatewayUnavailable, MatriksGatewayClient
from app.services.scanner import SymbolScanner
from app.services.stop_loss_guard import StopLossGuard, check_stop_loss_positions
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


async def _seed_position(symbol: str, qty: float) -> None:
    async with async_session_factory() as session:
        session.add(BotPosition(symbol=symbol, qty=qty, avg_price=100.0))
        await session.commit()


async def _seed_buy_stop(
    symbol: str, stop_loss: float, *, allow_order: bool = True
) -> None:
    async with async_session_factory() as session:
        session.add(
            RiskDecision(
                request_id=f"{symbol}-buy-seed",
                symbol=symbol,
                action=SignalAction.BUY.value,
                confidence=90.0,
                risk_score=10.0,
                allow_order=allow_order,
                stop_loss=stop_loss,
                order_type="LIMIT",
                qty=10,
                mode="DEMO_LIVE",
            )
        )
        await session.commit()


class TestCheckStopLossPositions:
    async def test_no_open_positions_returns_empty(self):
        fake = FakeGateway()
        assert await check_stop_loss_positions(make_gateway_client(fake)) == []

    async def test_no_recorded_stop_is_a_no_op(self):
        await _seed_position("THYAO", 10)
        fake = FakeGateway()
        # lastPrice defaults to 71.5 in the fake snapshot; no RiskDecision
        # seeded, so there is no stop to compare against.
        assert await check_stop_loss_positions(make_gateway_client(fake)) == []

    async def test_price_above_stop_does_not_trigger(self):
        await _seed_position("THYAO", 10)
        await _seed_buy_stop("THYAO", stop_loss=60.0)
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {"lastPrice": 71.5}

        assert await check_stop_loss_positions(make_gateway_client(fake)) == []

    async def test_price_at_or_below_stop_triggers_exit_full_sell(self):
        await _seed_position("THYAO", 10)
        await _seed_buy_stop("THYAO", stop_loss=68.0)
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {"lastPrice": 68.0}

        triggered = await check_stop_loss_positions(make_gateway_client(fake))

        assert len(triggered) == 1
        response = triggered[0].response
        assert response.symbol == "THYAO"
        assert response.action == SignalAction.SELL
        assert response.qty == 10
        assert response.order_type.value == "LIMIT"
        assert response.allow_order is True
        assert response.requires_confirmation is False
        assert float(response.price) == 68.0

    async def test_partial_fill_position_sells_actual_held_qty(self):
        # Original decision sized a BUY for more than what actually filled;
        # bot_positions reflects the broker's real (partial) position.
        await _seed_position("THYAO", 3)
        await _seed_buy_stop("THYAO", stop_loss=68.0)
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {"lastPrice": 65.0}

        triggered = await check_stop_loss_positions(make_gateway_client(fake))

        assert len(triggered) == 1
        assert triggered[0].response.qty == 3

    async def test_ignores_rejected_buy_decision_stop(self):
        await _seed_position("THYAO", 10)
        await _seed_buy_stop("THYAO", stop_loss=68.0, allow_order=False)
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {"lastPrice": 65.0}

        assert await check_stop_loss_positions(make_gateway_client(fake)) == []

    async def test_gateway_unavailable_for_one_symbol_skips_it_only(self):
        await _seed_position("THYAO", 10)
        await _seed_buy_stop("THYAO", stop_loss=68.0)

        class RaisingGateway:
            async def get_snapshot(self, symbol: str):
                raise GatewayUnavailable("gateway down")

        triggered = await check_stop_loss_positions(RaisingGateway())

        assert triggered == []

    async def test_most_recent_buy_decision_stop_is_used(self):
        await _seed_position("THYAO", 10)
        await _seed_buy_stop("THYAO", stop_loss=60.0)
        await asyncio.sleep(0)
        # A more recent BUY decision recorded a tighter stop.
        async with async_session_factory() as session:
            session.add(
                RiskDecision(
                    request_id="THYAO-buy-seed-2",
                    symbol="THYAO",
                    action=SignalAction.BUY.value,
                    confidence=90.0,
                    risk_score=10.0,
                    allow_order=True,
                    stop_loss=70.0,
                    order_type="LIMIT",
                    qty=10,
                    mode="DEMO_LIVE",
                    created_at=datetime.now(timezone.utc) + timedelta(seconds=1),
                )
            )
            await session.commit()
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {"lastPrice": 69.0}

        triggered = await check_stop_loss_positions(make_gateway_client(fake))

        assert len(triggered) == 1
        assert float(triggered[0].response.stop_loss) == 70.0


class TestStopLossGuardCooldown:
    def test_not_cooling_down_before_trigger(self):
        guard = StopLossGuard()
        assert guard.is_symbol_cooling_down("THYAO") is False

    def test_cooling_down_after_trigger(self):
        guard = StopLossGuard()
        guard.mark_triggered("thyao")
        assert guard.is_symbol_cooling_down("THYAO") is True

    def test_unrelated_symbol_not_affected(self):
        guard = StopLossGuard()
        guard.mark_triggered("THYAO")
        assert guard.is_symbol_cooling_down("AKBNK") is False


class TestScannerStopLossIntegration:
    """Dispatch through the real order path so kill switch/cooldown gates apply."""

    @pytest.fixture(autouse=True)
    def _seed_account_policy(self):
        async def seed():
            async with async_session_factory() as session:
                session.add(
                    SystemConfig(
                        key="accountReservationHandling",
                        value="BACKEND_DEDUCTED",
                        value_type="reservation_handling",
                        description="test account policy",
                    )
                )
                session.add(
                    TradeWatchlistSymbol(
                        symbol="THYAO",
                        is_active=True,
                        source="MANUAL_OVERRIDE",
                        manual_override=True,
                    )
                )
                await session.commit()

        asyncio.run(seed())
        guard_module.stop_loss_guard._triggered_on.clear()
        yield
        guard_module.stop_loss_guard._triggered_on.clear()

    async def test_triggered_stop_sends_sell_order(self, monkeypatch):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)
        monkeypatch.setattr(scanner_module.settings, "default_mode", Mode.DEMO_LIVE)
        await _seed_position("THYAO", 10)
        await _seed_buy_stop("THYAO", stop_loss=68.0)
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {
            "lastPrice": 65.0,
            "bidPrice": 64.95,
            "askPrice": 65.05,
            "bestBid": 64.95,
        }
        scanner = SymbolScanner(gateway=make_gateway_client(fake))

        await scanner._run_stop_loss_guard()

        assert len(fake.orders) == 1
        assert fake.orders[0]["symbol"] == "THYAO"
        assert fake.orders[0]["side"] == "SELL"
        assert fake.orders[0]["qty"] == 10
        assert guard_module.stop_loss_guard.is_symbol_cooling_down("THYAO") is True

    async def test_kill_switch_blocks_stop_loss_order(self, monkeypatch):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)

        async def kill_switch_on(_session) -> bool:
            return True

        monkeypatch.setattr(scanner_module, "is_kill_switch_enabled", kill_switch_on)
        await _seed_position("THYAO", 10)
        await _seed_buy_stop("THYAO", stop_loss=68.0)
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {"lastPrice": 65.0}
        scanner = SymbolScanner(gateway=make_gateway_client(fake))

        await scanner._run_stop_loss_guard()

        assert fake.orders == []

    async def test_orders_disabled_does_not_send(self, monkeypatch):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", False)
        await _seed_position("THYAO", 10)
        await _seed_buy_stop("THYAO", stop_loss=68.0)
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {"lastPrice": 65.0}
        scanner = SymbolScanner(gateway=make_gateway_client(fake))

        await scanner._run_stop_loss_guard()

        assert fake.orders == []

    async def test_no_breach_sends_nothing(self, monkeypatch):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)
        await _seed_position("THYAO", 10)
        await _seed_buy_stop("THYAO", stop_loss=60.0)
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {"lastPrice": 71.5}
        scanner = SymbolScanner(gateway=make_gateway_client(fake))

        await scanner._run_stop_loss_guard()

        assert fake.orders == []
        assert guard_module.stop_loss_guard.is_symbol_cooling_down("THYAO") is False
