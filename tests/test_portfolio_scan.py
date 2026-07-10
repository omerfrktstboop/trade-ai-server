"""Tests for the portfolio re-evaluation loop (Task 5) and positionContext."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.risk_config import RiskConfig
from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import BotPosition
from app.models.signal import SignalMode, SignalRequest
from app.services import scanner as scanner_module
from app.services.scanner import SymbolScanner
from tests.fake_gateway import FakeGateway
from app.services.matriks_gateway import MatriksGatewayClient


@pytest.fixture(autouse=True)
def _reset_db():
    asyncio.run(drop_all())
    asyncio.run(init_db())
    yield
    asyncio.run(drop_all())
    asyncio.run(init_db())


def make_gateway_client(fake: FakeGateway) -> MatriksGatewayClient:
    return MatriksGatewayClient(
        base_url="http://fake-gateway", token=fake.token, transport=fake.transport
    )


def _cfg(**kwargs: Any) -> RiskConfig:
    defaults: dict = dict(
        allowed_symbols="THYAO",
        locked_long_term_symbols="ASELS",
        disable_trading_after="23:59",
        timezone="Etc/GMT+12",
    )
    defaults.update(kwargs)
    return RiskConfig(**defaults, _env_file=None)


@pytest.fixture
def runtime_stubs(monkeypatch):
    state = {"kill_switch": False, "config": _cfg(), "scan_interval": 30, "overrides": []}

    async def fake_kill_switch(_s):
        return state["kill_switch"]

    async def fake_runtime_config(_s):
        return state["config"]

    class _Profile:
        scan_interval_minutes = 30

    async def fake_profile(_s):
        return _Profile()

    async def fake_overrides(_s):
        return state["overrides"]

    monkeypatch.setattr(scanner_module, "is_kill_switch_enabled", fake_kill_switch)
    monkeypatch.setattr(scanner_module, "build_runtime_risk_config", fake_runtime_config)
    monkeypatch.setattr(scanner_module, "get_active_profile", fake_profile)
    monkeypatch.setattr(scanner_module, "list_pending_override_symbols", fake_overrides)
    return state


async def _add_position(symbol: str, qty: float, avg_price: float | None = None):
    async with async_session_factory() as session:
        session.add(BotPosition(symbol=symbol, qty=qty, avg_price=avg_price))
        await session.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# Portfolio scan döngüsü
# ═══════════════════════════════════════════════════════════════════════════════


class TestPortfolioScan:
    async def test_held_position_outside_watchlist_is_evaluated(
        self, monkeypatch, runtime_stubs
    ):
        """allowedSymbols'te olmayan pozisyonlu sembol de portföy turunda taranır."""
        await _add_position("OPX30F", qty=50.0, avg_price=10.0)

        calls: list[str] = []

        async def fake_evaluate(symbol: str, **kwargs: Any):
            calls.append(symbol)
            return None

        monkeypatch.setattr(scanner_module, "evaluate_symbol", fake_evaluate)
        # position_sync tick içinde gateway pozisyonlarıyla tabloyu ezmesin.
        async def _noop_sync(_gw):
            return None

        monkeypatch.setattr(scanner_module, "sync_positions_from_gateway", _noop_sync)

        scanner = SymbolScanner(gateway=make_gateway_client(FakeGateway()))
        await scanner.tick()

        assert "OPX30F" in calls

    async def test_portfolio_scan_respects_interval(self, monkeypatch, runtime_stubs):
        await _add_position("THYAO", qty=10.0, avg_price=300.0)

        calls: list[str] = []

        async def fake_evaluate(symbol: str, **kwargs: Any):
            calls.append(symbol)
            return None

        monkeypatch.setattr(scanner_module, "evaluate_symbol", fake_evaluate)

        async def _noop_sync(_gw):
            return None

        monkeypatch.setattr(scanner_module, "sync_positions_from_gateway", _noop_sync)

        scanner = SymbolScanner(gateway=make_gateway_client(FakeGateway()))
        await scanner.tick()
        first_count = calls.count("THYAO")

        await scanner.tick()  # interval dolmadı — portföy turu tekrarlanmaz

        # İkinci tick'te normal tarama da interval'e takılır; THYAO sayısı artmaz.
        assert calls.count("THYAO") == first_count

    async def test_no_positions_no_extra_calls(self, monkeypatch, runtime_stubs):
        calls: list[str] = []

        async def fake_evaluate(symbol: str, **kwargs: Any):
            calls.append(symbol)
            return None

        monkeypatch.setattr(scanner_module, "evaluate_symbol", fake_evaluate)

        async def _noop_sync(_gw):
            return None

        monkeypatch.setattr(scanner_module, "sync_positions_from_gateway", _noop_sync)

        scanner = SymbolScanner(gateway=make_gateway_client(FakeGateway()))
        await scanner.tick()

        # Yalnızca watchlist'teki THYAO taranır; portföy turu sembol eklemez.
        assert calls == ["THYAO"]


# ═══════════════════════════════════════════════════════════════════════════════
# positionContext (evaluator)
# ═══════════════════════════════════════════════════════════════════════════════


class TestPositionContext:
    async def test_position_context_added_to_payload(self):
        from app.services.evaluator import _build_position_context

        await _add_position("THYAO", qty=100.0, avg_price=320.0)
        req = SignalRequest(
            requestId="t-1",
            symbol="THYAO",
            timeframe="Min5",
            lastPrice=336.0,
            open=330.0,
            high=340.0,
            low=328.0,
            volume=1000.0,
            rsi=55.0,
            mode=SignalMode.PAPER,
            botPositionQty=100.0,
        )

        ctx = await _build_position_context(req)

        assert ctx is not None
        assert ctx["qty"] == 100.0
        assert ctx["avgCost"] == 320.0
        assert ctx["currentPrice"] == 336.0
        assert ctx["unrealizedPnlPct"] == 5.0  # (336-320)/320
        assert ctx["positionValueTl"] == 33600.0

    async def test_no_position_returns_none(self):
        from app.services.evaluator import _build_position_context

        req = SignalRequest(
            requestId="t-2",
            symbol="THYAO",
            timeframe="Min5",
            lastPrice=336.0,
            open=330.0,
            high=340.0,
            low=328.0,
            volume=1000.0,
            rsi=55.0,
            mode=SignalMode.PAPER,
            botPositionQty=0.0,
        )

        assert await _build_position_context(req) is None

    async def test_missing_avg_price_still_returns_context(self):
        from app.services.evaluator import _build_position_context

        await _add_position("AKBNK", qty=25.0, avg_price=None)
        req = SignalRequest(
            requestId="t-3",
            symbol="AKBNK",
            timeframe="Min5",
            lastPrice=70.0,
            open=70.0,
            high=71.0,
            low=69.0,
            volume=1000.0,
            rsi=50.0,
            mode=SignalMode.PAPER,
            botPositionQty=25.0,
        )

        ctx = await _build_position_context(req)

        assert ctx is not None
        assert ctx["avgCost"] is None
        assert "unrealizedPnlPct" not in ctx
