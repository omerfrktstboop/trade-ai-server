"""Integration tests for the signal evaluate endpoint flow.

Tests the full chain:  strategy → RiskEngine → SignalResponse
"""

from __future__ import annotations

import pytest

from app.core.risk_config import RiskConfig
from app.models.signal import SignalAction, SignalMode, SignalRequest
from app.services.risk_engine import RiskEngine
from app.services.strategy import generate_dummy_decision


# ── Helpers ───────────────────────────────────────────────────────────────────


def _req(**kwargs) -> SignalRequest:
    """Create a minimal SignalRequest with defaults good for testing."""
    defaults: dict = dict(
        requestId="test-001",
        symbol="THYAO",
        timeframe="1h",
        lastPrice=100.0,
        open=99.0,
        high=102.0,
        low=98.0,
        volume=1000.0,
        rsi=50.0,
        ema20=98.0,
        ema50=95.0,
        mode=SignalMode.MANUAL,
    )
    defaults.update(kwargs)
    return SignalRequest(**defaults)


def _cfg(**kwargs) -> RiskConfig:
    defaults: dict = dict(
        allowed_symbols="THYAO,AKBNK,SISE,KCHOL,TUPRS",
        locked_long_term_symbols="ASELS,EREGL",
        max_position_value_per_symbol=5000,
        min_confidence_for_buy=75,
        min_confidence_for_sell=70,
        allow_sell_long_term=False,
    )
    defaults.update(kwargs)
    return RiskConfig(**defaults, _env_file="")


# ── Strategy tests ────────────────────────────────────────────────────────────


class TestDummyStrategy:
    """Test that generate_dummy_decision produces correct raw signals."""

    def test_buy_signal_oversold_price_above_ema(self):
        """RSI < 35 + price > ema20 → BUY."""
        req = _req(rsi=30.0, lastPrice=100.0, ema20=95.0)
        dec = generate_dummy_decision(req)
        assert dec.action == SignalAction.BUY
        assert dec.confidence > 60
        assert dec.qty > 0

    def test_sell_signal_overbought_with_position(self):
        """RSI > 75 + botPositionQty > 0 → SELL."""
        req = _req(rsi=80.0, botPositionQty=10)
        dec = generate_dummy_decision(req)
        assert dec.action == SignalAction.SELL
        assert dec.qty > 0
        assert dec.qty <= 10  # can't sell more than held

    def test_wait_when_no_rule_matches(self):
        """Neutral RSI → WAIT."""
        req = _req(rsi=50.0)
        dec = generate_dummy_decision(req)
        assert dec.action == SignalAction.WAIT

    def test_wait_when_rsi_none(self):
        """Missing RSI → neutral → WAIT."""
        req = _req(rsi=None)
        dec = generate_dummy_decision(req)
        assert dec.action == SignalAction.WAIT


# ── End-to-end flow tests (strategy + RiskEngine) ─────────────────────────────


class TestEndToEndFlow:
    """Full pipeline: request → strategy → RiskEngine → response."""

    def test_buy_goes_through_with_good_confidence(self):
        """Strong BUY signal + high confidence → allow_order=True."""
        engine = RiskEngine(_cfg())
        req = _req(
            rsi=15.0,   # confidence = 95-15 = 80 ≥ 75 threshold
            lastPrice=100.0,
            ema20=90.0,
            mode=SignalMode.LIVE,
        )
        dec = generate_dummy_decision(req)
        resp = engine.evaluate(req, dec)

        assert resp.action == SignalAction.BUY
        assert resp.allow_order is True
        assert resp.qty > 0

    def test_sell_goes_through_with_good_confidence(self):
        """Overbought + has position → SELL allowed."""
        engine = RiskEngine(_cfg())
        req = _req(
            rsi=85.0,
            botPositionQty=20,
            mode=SignalMode.LIVE,
        )
        dec = generate_dummy_decision(req)
        resp = engine.evaluate(req, dec)

        assert resp.action == SignalAction.SELL
        assert resp.allow_order is True

    def test_paper_mode_overrides_buy(self):
        """PAPER mode forces allow_order=False even for strong BUY."""
        engine = RiskEngine(_cfg())
        req = _req(
            rsi=25.0,
            lastPrice=100.0,
            ema20=90.0,
            mode=SignalMode.PAPER,
        )
        dec = generate_dummy_decision(req)
        resp = engine.evaluate(req, dec)

        assert resp.allow_order is False
        assert "PAPER mode" in resp.reason

    def test_wait_is_safe_default(self):
        """Neutral RSI → WAIT with allow_order=False."""
        engine = RiskEngine(_cfg())
        req = _req(rsi=50.0, mode=SignalMode.LIVE)
        dec = generate_dummy_decision(req)
        resp = engine.evaluate(req, dec)

        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False

    def test_unknown_symbol_blocked(self):
        """Disallowed symbol → RiskEngine overrides to WAIT."""
        engine = RiskEngine(_cfg())
        req = _req(
            symbol="GARAN",
            rsi=25.0,
            lastPrice=100.0,
            ema20=90.0,
            mode=SignalMode.LIVE,
        )
        dec = generate_dummy_decision(req)  # strategy says BUY
        assert dec.action == SignalAction.BUY  # raw decision is BUY

        resp = engine.evaluate(req, dec)  # risk engine blocks it
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False
        assert "not in the allowed list" in resp.reason

    def test_low_confidence_buy_blocked(self):
        """BUY with confidence just below threshold → blocked."""
        engine = RiskEngine(_cfg(min_confidence_for_buy=75))
        # RSI=34 → confidence ≈ 61 (under 75)
        req = _req(
            rsi=34.0,
            lastPrice=100.0,
            ema20=90.0,
            mode=SignalMode.LIVE,
        )
        dec = generate_dummy_decision(req)
        assert dec.action == SignalAction.BUY
        assert dec.confidence < 75.0

        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert "Confidence" in resp.reason

    def test_sell_blocked_when_no_position(self):
        """RSI > 75 but botPositionQty=0 → strategy says WAIT (no sell)."""
        req = _req(rsi=80.0, botPositionQty=0, mode=SignalMode.LIVE)
        dec = generate_dummy_decision(req)
        assert dec.action == SignalAction.WAIT  # strategy won't suggest sell
