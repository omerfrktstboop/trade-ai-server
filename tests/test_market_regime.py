"""Tests for the index market-regime service and the RiskEngine macro filter."""

from __future__ import annotations

import pytest

from app.core.risk_config import RiskConfig
from app.models.signal import EntryRange, SignalAction, SignalMode, SignalRequest
from app.services import market_regime as mr
from app.services.market_regime import _classify, get_index_regime
from app.services.matriks_gateway import GatewayUnavailable
from app.services.risk_engine import RiskDecision, RiskEngine


@pytest.fixture(autouse=True)
def _fresh_cache():
    mr.reset_cache()
    yield
    mr.reset_cache()


class FakeGateway:
    def __init__(self, payload=None, raise_exc=None):
        self._payload = payload or {}
        self._raise = raise_exc

    async def get_snapshot(self, symbol: str):
        if self._raise is not None:
            raise self._raise
        return {"ok": True, "symbol": symbol, "payload": self._payload}


# ═══════════════════════════════════════════════════════════════════════════════
# _classify — saf sınıflandırma
# ═══════════════════════════════════════════════════════════════════════════════


class TestClassify:
    def test_price_below_both_emas_is_downtrend(self):
        payload = {"lastPrice": 9000.0, "ema20": 9200.0, "ema50": 9400.0}
        assert _classify(payload) == "DOWNTREND"

    def test_gateway_regime_passthrough_high_volatility(self):
        payload = {
            "lastPrice": 9500.0,
            "ema20": 9400.0,
            "ema50": 9300.0,
            "marketRegime": "HIGH_VOLATILITY",
        }
        assert _classify(payload) == "HIGH_VOLATILITY"

    def test_missing_emas_falls_back_to_gateway_regime(self):
        assert _classify({"lastPrice": 9500.0, "marketRegime": "TRENDING"}) == "TRENDING"

    def test_empty_payload_is_unknown(self):
        assert _classify({}) == "UNKNOWN"


# ═══════════════════════════════════════════════════════════════════════════════
# get_index_regime — cache + fail-open
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetIndexRegime:
    @pytest.fixture(autouse=True)
    def _index_symbol(self, monkeypatch):
        """conftest testlerde endeksi kapatır (MARKET_INDEX_SYMBOL="");
        bu suite gerçek endeks akışını test ettiği için geri açar."""
        monkeypatch.setattr(
            "app.services.market_regime.settings.market_index_symbol", "XU100"
        )

    async def test_returns_downtrend_from_index_snapshot(self):
        gw = FakeGateway({"lastPrice": 9000.0, "ema20": 9200.0, "ema50": 9400.0})
        assert await get_index_regime(gw) == "DOWNTREND"

    async def test_gateway_error_returns_unknown(self):
        gw = FakeGateway(raise_exc=GatewayUnavailable("down"))
        assert await get_index_regime(gw) == "UNKNOWN"

    async def test_empty_index_symbol_disables_filter(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.market_regime.settings.market_index_symbol", ""
        )
        gw = FakeGateway(raise_exc=RuntimeError("should not be called"))
        assert await get_index_regime(gw) == "UNKNOWN"

    async def test_result_is_cached_for_ttl(self):
        gw = FakeGateway({"lastPrice": 9000.0, "ema20": 9200.0, "ema50": 9400.0})
        first = await get_index_regime(gw)

        # İkinci çağrı cache'ten gelmeli — bozuk gateway fark edilmez.
        broken = FakeGateway(raise_exc=RuntimeError("should not be called"))
        second = await get_index_regime(broken)

        assert first == second == "DOWNTREND"


# ═══════════════════════════════════════════════════════════════════════════════
# RiskEngine makro filtre entegrasyonu
# ═══════════════════════════════════════════════════════════════════════════════


def _cfg(**kwargs) -> RiskConfig:
    defaults = dict(
        allowed_symbols="THYAO,AKBNK",
        locked_long_term_symbols="ASELS",
        disable_trading_after="23:59",
        timezone="Etc/GMT+12",
        min_confidence_for_buy=70.0,
        max_position_value_per_symbol=1_000_000.0,
        max_daily_trade_count=100,
    )
    defaults.update(kwargs)
    return RiskConfig(**defaults, _env_file=None)


def _req(mode=SignalMode.DEMO_LIVE) -> SignalRequest:
    return SignalRequest(
        requestId="t-1",
        symbol="THYAO",
        timeframe="Min5",
        lastPrice=100.0,
        open=99.0,
        high=101.0,
        low=98.0,
        volume=1000.0,
        rsi=35.0,
        mode=mode,
    )


def _buy(confidence=80.0) -> RiskDecision:
    return RiskDecision(
        action=SignalAction.BUY,
        confidence=confidence,
        qty=1.0,
        entry_range=EntryRange(min=99.0, max=100.0),
        stop_loss=97.0,
        target_price=106.0,
        reason="test buy",
    )


class TestMacroFilterInRiskEngine:
    def test_downtrend_blocks_buy(self):
        engine = RiskEngine(_cfg())
        resp = engine.evaluate(_req(), _buy(), market_regime="DOWNTREND")

        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False
        assert "DOWNTREND" in resp.reason

    def test_downtrend_does_not_block_sell(self):
        engine = RiskEngine(_cfg(min_confidence_for_sell=50.0))
        req = _req().model_copy(
            update={"bot_position_qty": 10.0, "total_account_qty": 10.0}
        )
        sell = RiskDecision(
            action=SignalAction.SELL, confidence=80.0, qty=5.0, reason="exit"
        )

        resp = engine.evaluate(req, sell, market_regime="DOWNTREND")

        assert resp.action == SignalAction.SELL

    def test_high_volatility_tightens_buy_threshold(self):
        engine = RiskEngine(_cfg(min_confidence_for_buy=70.0))

        # 80 güven: normalde geçer (70), HIGH_VOLATILITY'de eşik 85'e çıkar → düşer.
        resp = engine.evaluate(_req(), _buy(confidence=80.0), market_regime="HIGH_VOLATILITY")
        assert resp.allow_order is False

        # 90 güven: sertleşen eşiği de geçer.
        resp2 = engine.evaluate(_req(), _buy(confidence=90.0), market_regime="HIGH_VOLATILITY")
        assert resp2.allow_order is True

    def test_unknown_or_none_regime_applies_no_filter(self):
        engine = RiskEngine(_cfg())

        for regime in (None, "UNKNOWN", "NEUTRAL", "RANGE_LOW_VOLATILITY"):
            resp = engine.evaluate(_req(), _buy(confidence=80.0), market_regime=regime)
            assert resp.action == SignalAction.BUY, f"regime={regime}"
