"""Tests for the background scanner (app/services/scanner.py).

Fake gateway + monkeypatch'lenmiş evaluator/config yardımcılarıyla koşar.
"""

from __future__ import annotations

from typing import Any
import asyncio
from decimal import Decimal

import pytest

from app.config import Mode
from app.core.risk_config import RiskConfig
from app.models.signal import (
    EntryRange,
    OrderType,
    SignalAction,
    SignalMode,
    SignalResponse,
)
from app.services import scanner as scanner_module
from app.services.discovery_agent import DiscoveryScanResult
from app.services.evaluator import EvaluationResult
from app.services.matriks_gateway import GatewayUnavailable, MatriksGatewayClient
from app.services.scanner import SymbolScanner
from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import OrderCashReservation, SystemConfig, TradeWatchlistSymbol
from sqlalchemy import select
from tests.fake_gateway import FakeGateway


def make_result(
    *,
    symbol: str = "THYAO",
    action: SignalAction = SignalAction.BUY,
    allow_order: bool = True,
    requires_confirmation: bool = False,
    order_type: OrderType = OrderType.LIMIT,
    qty: float = 1.0,
    price: float | None = 71.5,
    mode: SignalMode = SignalMode.DEMO_LIVE,
) -> EvaluationResult:
    response = SignalResponse(
        requestId=f"{symbol}-20260709-120000-scan",
        symbol=symbol,
        action=action,
        qty=qty,
        orderType=order_type,
        price=price,
        confidenceScore=90.0,
        riskScore=10.0,
        allowOrder=allow_order,
        requiresConfirmation=requires_confirmation,
        reason="test",
        entryRange=EntryRange(min=70.0, max=71.5) if price else None,
        stopLoss=Decimal("68.00") if price else None,
        targetPrice=Decimal("75.00") if price else None,
    )
    return EvaluationResult(response=response, mode=mode)


def make_gateway_client(fake: FakeGateway) -> MatriksGatewayClient:
    return MatriksGatewayClient(
        base_url="http://fake-gateway", token=fake.token, transport=fake.transport
    )


def _cfg(**kwargs: Any) -> RiskConfig:
    defaults: dict = dict(
        allowed_symbols="THYAO,AKBNK",
        locked_long_term_symbols="ASELS",
        disable_trading_after="23:59",
        timezone="Etc/GMT+12",  # cutoff pratikte hiç geçmez
    )
    defaults.update(kwargs)
    return RiskConfig(**defaults, _env_file=None)


@pytest.fixture
def evaluated_symbols(monkeypatch) -> list[str]:
    """evaluate_symbol'ü kaydeden stub'la değiştirir."""
    calls: list[str] = []

    async def fake_evaluate(symbol: str, **kwargs: Any):
        calls.append(symbol)
        # SCANNER_ALLOW_ORDERS=false (default) → force_paper zorunlu
        assert kwargs.get("force_paper") is True
        return None

    monkeypatch.setattr(scanner_module, "evaluate_symbol", fake_evaluate)
    return calls


@pytest.fixture
def runtime_stubs(monkeypatch):
    """DB-bağımlı runtime config çağrılarını statik stub'larla değiştirir."""
    state = {
        "kill_switch": False,
        "config": _cfg(),
        "scan_interval": 30,
        "overrides": [],
        "trade_symbols": ["THYAO", "AKBNK"],
    }

    async def fake_kill_switch(_session) -> bool:
        return state["kill_switch"]

    async def fake_runtime_config(_session) -> RiskConfig:
        return state["config"]

    class _Profile:
        @property
        def scan_interval_minutes(self) -> int:
            return state["scan_interval"]

    async def fake_profile(_session) -> _Profile:
        return _Profile()

    async def fake_overrides(_session) -> list[str]:
        return state["overrides"]

    async def fake_trade_symbols() -> list[str]:
        return state["trade_symbols"]

    async def no_discovery(_self) -> None:
        return None

    async def no_research(_self, _declined_symbols: set[str]) -> None:
        return None

    async def no_refresh(_self) -> None:
        return None

    monkeypatch.setattr(scanner_module, "is_kill_switch_enabled", fake_kill_switch)
    monkeypatch.setattr(
        scanner_module, "build_runtime_risk_config", fake_runtime_config
    )
    monkeypatch.setattr(scanner_module, "get_active_profile", fake_profile)
    monkeypatch.setattr(scanner_module, "list_pending_override_symbols", fake_overrides)
    monkeypatch.setattr(
        scanner_module, "list_trade_eligible_symbols", fake_trade_symbols
    )
    monkeypatch.setattr(SymbolScanner, "_run_discovery", no_discovery)
    monkeypatch.setattr(SymbolScanner, "_run_research", no_research)
    monkeypatch.setattr(SymbolScanner, "_refresh_pipeline_status", no_refresh)
    return state


# ═══════════════════════════════════════════════════════════════════════════════
# Tick davranışı
# ═══════════════════════════════════════════════════════════════════════════════


class TestScannerTick:
    async def test_due_symbols_evaluated_in_paper(
        self, evaluated_symbols, runtime_stubs, caplog
    ):
        caplog.set_level("INFO", logger=scanner_module.__name__)
        fake = FakeGateway()
        scanner = SymbolScanner(gateway=make_gateway_client(fake))

        result = await scanner.tick()

        assert result == ["THYAO", "AKBNK"]
        assert evaluated_symbols == ["THYAO", "AKBNK"]
        assert "TRADING_SCAN_COMPLETED evaluatedCount=2" in caplog.text

    async def test_second_tick_within_interval_skips_symbols(
        self, evaluated_symbols, runtime_stubs
    ):
        fake = FakeGateway()
        scanner = SymbolScanner(gateway=make_gateway_client(fake))

        await scanner.tick()
        result = await scanner.tick()

        assert result == []
        assert evaluated_symbols == ["THYAO", "AKBNK"]

    async def test_pending_override_bypasses_interval(
        self, evaluated_symbols, runtime_stubs
    ):
        fake = FakeGateway()
        scanner = SymbolScanner(gateway=make_gateway_client(fake))

        await scanner.tick()
        runtime_stubs["overrides"] = ["THYAO"]
        result = await scanner.tick()

        assert result == ["THYAO"]
        assert evaluated_symbols == ["THYAO", "AKBNK", "THYAO"]

    async def test_pending_portfolio_symbol_outside_watchlist_is_evaluated(
        self, evaluated_symbols, runtime_stubs
    ):
        runtime_stubs["overrides"] = ["OPT25F"]
        fake = FakeGateway()
        scanner = SymbolScanner(gateway=make_gateway_client(fake))

        result = await scanner.tick()

        assert result == ["THYAO", "AKBNK", "OPT25F"]
        assert evaluated_symbols == ["THYAO", "AKBNK", "OPT25F"]

    async def test_scanner_uses_configured_default_mode_when_no_db_override(
        self, runtime_stubs, monkeypatch
    ):
        calls: list[SignalMode] = []

        async def fake_evaluate(_symbol: str, **kwargs: Any):
            calls.append(kwargs["mode"])
            assert kwargs["force_paper"] is False
            return None

        monkeypatch.setattr(scanner_module, "evaluate_symbol", fake_evaluate)
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)
        monkeypatch.setattr(
            scanner_module.settings,
            "default_mode",
            Mode.DEMO_LIVE,
        )
        scanner = SymbolScanner(gateway=make_gateway_client(FakeGateway()))

        await scanner.tick()

        assert calls == [SignalMode.DEMO_LIVE, SignalMode.DEMO_LIVE]


class TestScannerLifecycleLogs:
    async def test_discovery_gateway_status_is_logged(self, monkeypatch, caplog):
        class _Policy:
            discovery_interval_minutes = 1

        scanner = SymbolScanner(gateway=make_gateway_client(FakeGateway()))

        async def fake_policy() -> _Policy:
            return _Policy()

        async def unavailable_discovery(_gateway):
            return DiscoveryScanResult(status="GATEWAY_UNAVAILABLE")

        monkeypatch.setattr(scanner_module, "load_research_policy", fake_policy)
        monkeypatch.setattr(scanner_module, "run_discovery_scan", unavailable_discovery)
        caplog.set_level("INFO", logger=scanner_module.__name__)

        await scanner._run_discovery()

        assert "DISCOVERY_SKIPPED_GATEWAY_UNAVAILABLE" in caplog.text

    async def test_research_completion_is_logged(self, monkeypatch, caplog):
        scanner = SymbolScanner(gateway=make_gateway_client(FakeGateway()))

        async def fake_research(_gateway):
            return ["GARAN"]

        async def fake_maintenance(_declined_symbols: set[str]):
            return ["AKBNK"]

        monkeypatch.setattr(scanner_module, "run_research_cycle", fake_research)
        monkeypatch.setattr(
            scanner_module, "maintain_trade_watchlist", fake_maintenance
        )
        caplog.set_level("INFO", logger=scanner_module.__name__)

        await scanner._run_research(set())

        assert (
            "RESEARCH_COMPLETED evaluatedCount=1 watchlistRemovedCount=1" in caplog.text
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Güvenlik kapıları
# ═══════════════════════════════════════════════════════════════════════════════


class TestScannerGates:
    async def test_kill_switch_skips_cycle(self, evaluated_symbols, runtime_stubs):
        runtime_stubs["kill_switch"] = True
        fake = FakeGateway()
        scanner = SymbolScanner(gateway=make_gateway_client(fake))

        result = await scanner.tick()

        assert result == []
        assert evaluated_symbols == []

    async def test_cutoff_passed_skips_cycle(self, evaluated_symbols, runtime_stubs):
        # Etc/GMT-14 + 00:00 cutoff → her zaman geçmiş durumda
        runtime_stubs["config"] = _cfg(
            disable_trading_after="00:00", timezone="Etc/GMT-14"
        )
        fake = FakeGateway()
        scanner = SymbolScanner(gateway=make_gateway_client(fake))

        result = await scanner.tick()

        assert result == []
        assert evaluated_symbols == []

    async def test_market_data_pipeline_runs_after_trading_cutoff(
        self, evaluated_symbols, runtime_stubs, monkeypatch
    ):
        """Cutoff blocks trade/order/portfolio paths, not discovery research."""
        runtime_stubs["config"] = _cfg(
            disable_trading_after="00:00", timezone="Etc/GMT-14"
        )
        discovery_calls: list[bool] = []
        research_calls: list[set[str]] = []
        refresh_calls: list[bool] = []
        timeout_calls: list[bool] = []
        portfolio_calls: list[set[str]] = []

        async def fake_discovery(_self) -> None:
            discovery_calls.append(True)

        async def fake_research(_self, declined_symbols: set[str]) -> None:
            research_calls.append(declined_symbols)

        async def fake_refresh(_self) -> None:
            refresh_calls.append(True)

        async def fake_timeout(_self) -> None:
            timeout_calls.append(True)

        async def fake_portfolio(_self, overrides: set[str]) -> None:
            portfolio_calls.append(overrides)

        monkeypatch.setattr(SymbolScanner, "_run_discovery", fake_discovery)
        monkeypatch.setattr(SymbolScanner, "_run_research", fake_research)
        monkeypatch.setattr(SymbolScanner, "_refresh_pipeline_status", fake_refresh)
        monkeypatch.setattr(SymbolScanner, "_run_order_timeout_check", fake_timeout)
        monkeypatch.setattr(SymbolScanner, "_run_portfolio_scan", fake_portfolio)
        scanner = SymbolScanner(gateway=make_gateway_client(FakeGateway()))

        result = await scanner.tick()

        assert result == []
        assert evaluated_symbols == []
        assert discovery_calls == [True]
        assert research_calls == [set()]
        assert refresh_calls == [True]
        assert timeout_calls == []
        assert portfolio_calls == []

    async def test_gateway_unavailable_skips_cycle(
        self, evaluated_symbols, runtime_stubs
    ):
        import httpx

        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = MatriksGatewayClient(
            base_url="http://fake-gateway",
            token="x",
            transport=httpx.MockTransport(refuse),
        )
        scanner = SymbolScanner(gateway=client)

        result = await scanner.tick()

        assert result == []
        assert evaluated_symbols == []

    async def test_discovery_runs_when_positions_are_not_loaded(
        self, evaluated_symbols, runtime_stubs, monkeypatch, caplog
    ):
        """Research path needs snapshots, but must not reach order-capable scans."""
        discovery_calls: list[bool] = []
        research_calls: list[set[str]] = []
        refresh_calls: list[bool] = []
        timeout_calls: list[bool] = []
        caplog.set_level("INFO", logger=scanner_module.__name__)

        async def fake_discovery(_self) -> None:
            discovery_calls.append(True)

        async def fake_research(_self, declined_symbols: set[str]) -> None:
            research_calls.append(declined_symbols)

        async def fake_refresh(_self) -> None:
            refresh_calls.append(True)

        async def fake_timeout(_self) -> None:
            timeout_calls.append(True)

        async def fake_gateway_notification(_message: str) -> None:
            return None

        monkeypatch.setattr(SymbolScanner, "_run_discovery", fake_discovery)
        monkeypatch.setattr(SymbolScanner, "_run_research", fake_research)
        monkeypatch.setattr(SymbolScanner, "_refresh_pipeline_status", fake_refresh)
        monkeypatch.setattr(SymbolScanner, "_run_order_timeout_check", fake_timeout)
        monkeypatch.setattr(
            scanner_module, "notify_gateway_event", fake_gateway_notification
        )
        fake = FakeGateway()
        fake.positions_loaded = False
        scanner = SymbolScanner(gateway=make_gateway_client(fake))

        result = await scanner.tick()

        assert result == []
        assert evaluated_symbols == []
        assert discovery_calls == [True]
        assert research_calls == [set()]
        assert refresh_calls == [True]
        assert timeout_calls == []
        assert "TRADING_SKIPPED_POSITIONS_NOT_LOADED" in caplog.text

    async def test_evaluation_error_does_not_stop_other_symbols(
        self, runtime_stubs, monkeypatch
    ):
        calls: list[str] = []

        async def flaky_evaluate(symbol: str, **kwargs: Any):
            calls.append(symbol)
            if symbol == "THYAO":
                raise RuntimeError("boom")
            return None

        monkeypatch.setattr(scanner_module, "evaluate_symbol", flaky_evaluate)
        fake = FakeGateway()
        scanner = SymbolScanner(gateway=make_gateway_client(fake))

        result = await scanner.tick()

        # THYAO patladı ama AKBNK değerlendirildi
        assert calls == ["THYAO", "AKBNK"]
        assert result == ["AKBNK"]

    async def test_gateway_unavailable_mid_cycle_stops_tick(
        self, runtime_stubs, monkeypatch
    ):
        calls: list[str] = []

        async def dying_evaluate(symbol: str, **kwargs: Any):
            calls.append(symbol)
            raise GatewayUnavailable("gateway died")

        monkeypatch.setattr(scanner_module, "evaluate_symbol", dying_evaluate)
        fake = FakeGateway()
        scanner = SymbolScanner(gateway=make_gateway_client(fake))

        result = await scanner.tick()

        # İlk sembolde gateway öldü → kalan semboller denenmez
        assert calls == ["THYAO"]
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# Emir yolu (Phase 2 — _maybe_send_order)
# ═══════════════════════════════════════════════════════════════════════════════


class TestOrderPath:
    """_maybe_send_order kapıları. DB persist'i (order_logs) burada stub'lanır —
    dev SQLite'a bağımlı olmasın diye."""

    @pytest.fixture(autouse=True)
    def no_db_persist(self, monkeypatch):
        asyncio.run(drop_all())
        asyncio.run(init_db())

        async def seed_account_policy():
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

        asyncio.run(seed_account_policy())
        self.persisted: list[tuple[str, str]] = []

        async def fake_persist(scanner_self, response, status, reason):
            self.persisted.append((status, reason))

        monkeypatch.setattr(SymbolScanner, "_persist_order_outcome", fake_persist)
        yield

    def make_scanner(self, fake: FakeGateway) -> SymbolScanner:
        return SymbolScanner(gateway=make_gateway_client(fake))

    async def test_orders_disabled_sends_nothing(self, monkeypatch):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", False)
        fake = FakeGateway()

        await self.make_scanner(fake)._maybe_send_order(make_result())

        assert fake.orders == []

    async def test_demo_live_buy_sends_order(self, monkeypatch):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)
        fake = FakeGateway()
        fake.positions = []

        await self.make_scanner(fake)._maybe_send_order(make_result())

        assert len(fake.orders) == 1
        sent = fake.orders[0]
        assert sent["symbol"] == "THYAO"
        assert sent["side"] == "BUY"
        assert sent["qty"] == 1.0
        assert sent["limitPrice"] == "71.5"
        assert sent["mode"] == "DEMO_LIVE"
        assert self.persisted == [
            (
                "SENT_PENDING",
                "Limit order SENT_PENDING; final status will be reported by OnOrderUpdate",
            )
        ]

    async def test_research_candidate_outside_trade_watchlist_cannot_send_buy(
        self, monkeypatch
    ):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)
        fake = FakeGateway(symbols=["THYAO", "GARAN"])

        await self.make_scanner(fake)._maybe_send_order(make_result(symbol="GARAN"))

        assert fake.orders == []

    async def test_order_time_sizing_can_only_reduce_qty(self, monkeypatch):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)
        fake = FakeGateway()
        fake.positions = []
        fake.account_payload["account"]["OrderableCash"] = "300"

        await self.make_scanner(fake)._maybe_send_order(make_result(qty=3))

        assert len(fake.orders) == 1
        assert fake.orders[0]["qty"] == 1

    async def test_order_time_sizing_never_increases_original_qty(self, monkeypatch):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)
        fake = FakeGateway()
        fake.positions = []
        fake.account_payload["account"]["OrderableCash"] = "50000"

        await self.make_scanner(fake)._maybe_send_order(make_result(qty=1))

        assert len(fake.orders) == 1
        assert fake.orders[0]["qty"] == 1

    async def test_buy_blocks_when_cash_becomes_insufficient(self, monkeypatch):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)
        fake = FakeGateway()
        fake.positions = []
        fake.account_payload["account"]["OrderableCash"] = "100"

        await self.make_scanner(fake)._maybe_send_order(make_result(qty=1))

        assert fake.orders == []

    async def test_buy_blocks_stale_account_data(self, monkeypatch):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)
        fake = FakeGateway()
        fake.positions = []
        fake.account_payload["accountDataAgeSeconds"] = "61"

        await self.make_scanner(fake)._maybe_send_order(make_result())

        assert fake.orders == []

    async def test_buy_blocks_when_position_price_snapshot_is_missing(
        self, monkeypatch
    ):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)
        fake = FakeGateway()
        fake.positions = [{"symbol": "UNMAPPED", "accountNetQty": 1}]

        await self.make_scanner(fake)._maybe_send_order(make_result())

        assert fake.orders == []

    async def test_gateway_send_uncertainty_keeps_cash_reserved(self, monkeypatch):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)
        fake = FakeGateway()
        fake.positions = []
        fake.order_transport_error = True

        await self.make_scanner(fake)._maybe_send_order(make_result())

        async with async_session_factory() as session:
            reservation = (
                await session.execute(select(OrderCashReservation))
            ).scalar_one()
        assert reservation.status == "SEND_UNKNOWN"
        assert reservation.remaining_qty == 1
        assert reservation.reserved_amount_tl == Decimal("71.5000000000")

    async def test_paper_mode_never_sends(self, monkeypatch):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)
        fake = FakeGateway()

        await self.make_scanner(fake)._maybe_send_order(
            make_result(mode=SignalMode.PAPER)
        )

        assert fake.orders == []

    async def test_real_live_blocked_in_phase2(self, monkeypatch):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)
        fake = FakeGateway()

        await self.make_scanner(fake)._maybe_send_order(
            make_result(mode=SignalMode.REAL_LIVE)
        )

        assert fake.orders == []

    async def test_allow_order_false_never_sends(self, monkeypatch):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)
        fake = FakeGateway()

        await self.make_scanner(fake)._maybe_send_order(make_result(allow_order=False))

        assert fake.orders == []

    async def test_requires_confirmation_never_sends(self, monkeypatch):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)
        fake = FakeGateway()

        await self.make_scanner(fake)._maybe_send_order(
            make_result(requires_confirmation=True)
        )

        assert fake.orders == []

    async def test_wait_action_never_sends(self, monkeypatch):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)
        fake = FakeGateway()

        await self.make_scanner(fake)._maybe_send_order(
            make_result(action=SignalAction.WAIT, allow_order=True)
        )

        assert fake.orders == []

    async def test_non_limit_order_type_never_sends(self, monkeypatch):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)
        fake = FakeGateway()

        await self.make_scanner(fake)._maybe_send_order(
            make_result(order_type=OrderType.MARKET)
        )

        assert fake.orders == []

    async def test_invalid_price_never_sends(self, monkeypatch):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)
        fake = FakeGateway()

        await self.make_scanner(fake)._maybe_send_order(make_result(price=None))

        assert fake.orders == []

    async def test_gateway_rejection_persisted(self, monkeypatch):
        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)
        fake = FakeGateway()
        fake.order_rejection = "EnableDemoOrders=false"

        await self.make_scanner(fake)._maybe_send_order(
            make_result(action=SignalAction.SELL)
        )

        assert len(fake.orders) == 1
        assert self.persisted == [("REJECTED", "EnableDemoOrders=false")]

    async def test_gateway_unreachable_during_preflight_creates_no_order(
        self, monkeypatch
    ):
        import httpx

        monkeypatch.setattr(scanner_module.settings, "scanner_allow_orders", True)

        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = MatriksGatewayClient(
            base_url="http://fake-gateway",
            token="x",
            transport=httpx.MockTransport(refuse),
        )
        scanner = SymbolScanner(gateway=client)

        await scanner._maybe_send_order(make_result())

        assert self.persisted == []


# ═══════════════════════════════════════════════════════════════════════════════
# Lifecycle
# ═══════════════════════════════════════════════════════════════════════════════


class TestScannerLifecycle:
    async def test_start_stop(self, evaluated_symbols, runtime_stubs):
        fake = FakeGateway()
        scanner = SymbolScanner(gateway=make_gateway_client(fake), tick_seconds=3600)

        scanner.start()
        assert scanner.running is True

        await scanner.stop()
        assert scanner.running is False

    async def test_double_start_is_noop(self, evaluated_symbols, runtime_stubs):
        fake = FakeGateway()
        scanner = SymbolScanner(gateway=make_gateway_client(fake), tick_seconds=3600)

        scanner.start()
        first_task = scanner._task
        scanner.start()

        assert scanner._task is first_task
        await scanner.stop()
