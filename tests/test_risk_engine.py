"""Unit tests for RiskEngine."""

from __future__ import annotations

import pytest

from app.core.risk_config import RiskConfig
from app.models.signal import (
    EntryRange,
    SignalAction,
    SignalMode,
    SignalRequest,
)
from app.services.risk_engine import RiskDecision, RiskEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(symbol: str = "THYAO", mode: SignalMode = SignalMode.MANUAL, **kwargs) -> SignalRequest:
    defaults: dict = dict(
        requestId="test-001",
        symbol=symbol,
        timeframe="1h",
        lastPrice=100.0,
        open=99.0,
        high=102.0,
        low=98.0,
        volume=1000.0,
        rsi=55.0,
        mode=mode,
    )
    defaults.update(kwargs)
    return SignalRequest(**defaults)


def _cfg(**kwargs) -> RiskConfig:
    defaults: dict = dict(
        allowed_symbols="THYAO,AKBNK,SISE,KCHOL,TUPRS",
        locked_long_term_symbols="ASELS,EREGL",
        max_position_value_per_symbol=3000,
        max_daily_trade_count=3,
        min_confidence_for_buy=75,
        min_confidence_for_sell=70,
        allow_sell_long_term=False,
        allow_short_selling=False,
        disable_trading_after="17:30",
    )
    defaults.update(kwargs)
    return RiskConfig(**defaults, _env_file="")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAllowedSymbols:
    """Check 1: Symbol not in allowedSymbols → WAIT."""

    def test_allowed_symbol_goes_through(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO")
        dec = RiskDecision(action=SignalAction.BUY, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.BUY

    def test_disallowed_symbol_blocked(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="GARAN")
        dec = RiskDecision(action=SignalAction.BUY, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False
        assert "not in the allowed list" in resp.reason

    def test_case_insensitive_symbol_lookup(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="thyao")
        dec = RiskDecision(action=SignalAction.BUY, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True


class TestLockedLongTerm:
    """Check 2: lockedLongTermSymbols block SELL."""

    def test_locked_symbol_sell_blocked(self):
        engine = RiskEngine(_cfg(allowed_symbols="ASELS,THYAO"))
        req = _make_request(symbol="ASELS", botPositionQty=10)
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False
        assert "locked long-term" in resp.reason.lower()

    def test_locked_symbol_sell_allowed_when_override(self):
        engine = RiskEngine(_cfg(allowed_symbols="ASELS,THYAO", allow_sell_long_term=True))
        req = _make_request(symbol="ASELS", botPositionQty=10)
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.SELL

    def test_locked_symbol_buy_goes_through(self):
        """BUY on locked symbol should not be blocked — lock is sell-only."""
        engine = RiskEngine(_cfg(allowed_symbols="ASELS,THYAO"))
        req = _make_request(symbol="ASELS")
        dec = RiskDecision(action=SignalAction.BUY, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.BUY


class TestSellPositionChecks:
    """Check 3: SELL needs botPositionQty > 0."""

    def test_sell_with_zero_position_blocked(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", botPositionQty=0)
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False
        assert "no bot position" in resp.reason.lower()

    def test_sell_with_position_succeeds(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", botPositionQty=10)
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.SELL


class TestSellQtyClamp:
    """Check 4: SELL qty ≤ botPositionQty."""

    def test_sell_qty_exceeds_position_clamped(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", botPositionQty=10)
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=20)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.SELL
        assert resp.qty == 10.0
        assert resp.allow_order is True
        assert "clamped" in resp.reason.lower()


class TestLockedLongTermQty:
    """Check 5: lockedLongTermQty never sellable."""

    def test_all_qty_locked_blocks_sell(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", botPositionQty=10, lockedLongTermQty=10)
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False
        assert "locked long-term" in resp.reason.lower()

    def test_partial_locked_sellable_qty_capped(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", botPositionQty=10, lockedLongTermQty=3)
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=10)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.SELL
        assert resp.qty == 7.0  # 10 - 3
        assert resp.allow_order is True


class TestPaperMode:
    """Check 6: PAPER mode always allowOrder=False."""

    def test_paper_mode_blocks_order(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", mode=SignalMode.PAPER)
        dec = RiskDecision(action=SignalAction.BUY, confidence=95.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert "PAPER mode" in resp.reason

    def test_paper_mode_even_with_perfect_confidence(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", mode=SignalMode.PAPER)
        dec = RiskDecision(action=SignalAction.BUY, confidence=100.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False

    def test_manual_mode_allows_order(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", mode=SignalMode.MANUAL)
        dec = RiskDecision(action=SignalAction.BUY, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True

    def test_live_mode_allows_order(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", mode=SignalMode.LIVE)
        dec = RiskDecision(action=SignalAction.BUY, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True


class TestConfidenceThreshold:
    """Check 7: Confidence below threshold → allowOrder=False."""

    def test_buy_below_threshold_blocked(self):
        engine = RiskEngine(_cfg(min_confidence_for_buy=75))
        req = _make_request(symbol="THYAO")
        dec = RiskDecision(action=SignalAction.BUY, confidence=70.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert "confidence" in resp.reason.lower()

    def test_buy_at_threshold_succeeds(self):
        engine = RiskEngine(_cfg(min_confidence_for_buy=75))
        req = _make_request(symbol="THYAO")
        dec = RiskDecision(action=SignalAction.BUY, confidence=75.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True

    def test_sell_below_threshold_blocked(self):
        engine = RiskEngine(_cfg(min_confidence_for_sell=70))
        req = _make_request(symbol="THYAO", botPositionQty=10)
        dec = RiskDecision(action=SignalAction.SELL, confidence=65.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False

    def test_sell_at_threshold_succeeds(self):
        engine = RiskEngine(_cfg(min_confidence_for_sell=70))
        req = _make_request(symbol="THYAO", botPositionQty=10)
        dec = RiskDecision(action=SignalAction.SELL, confidence=70.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True


class TestInvalidAction:
    """Check 8: Null/unknown action defaults to WAIT."""

    def test_null_decision_defaults_to_wait(self):
        engine = RiskEngine(_cfg())
        req = _make_request()
        resp = engine.evaluate(req)  # no decision
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False

    def test_wait_action_never_allows_order(self):
        engine = RiskEngine(_cfg())
        req = _make_request()
        dec = RiskDecision(action=SignalAction.WAIT, confidence=99.0)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False


class TestMaxPositionValue:
    """BUY value can't exceed maxPositionValuePerSymbol."""

    def test_buy_exceeds_max_value_blocked(self):
        engine = RiskEngine(_cfg(max_position_value_per_symbol=500))
        req = _make_request(symbol="THYAO", lastPrice=100)  # qty*100 > 500
        dec = RiskDecision(action=SignalAction.BUY, confidence=85.0, qty=6)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False

    def test_buy_within_limit_succeeds(self):
        engine = RiskEngine(_cfg(max_position_value_per_symbol=500))
        req = _make_request(symbol="THYAO", lastPrice=100)
        dec = RiskDecision(action=SignalAction.BUY, confidence=85.0, qty=5)  # 5*100 = 500
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
