"""Integration tests for the signal evaluate endpoint flow.

Tests the full chain:  SignalRequest → AiProvider → RiskDecision → RiskEngine → SignalResponse

With ``AI_PROVIDER=mock`` (default) the provider always returns WAIT,
so the endpoint is always safe regardless of the input.
"""

from __future__ import annotations

import pytest

from app.core.risk_config import RiskConfig
from app.models.signal import SignalAction, SignalMode, SignalRequest
from app.services.ai_provider import MockAiProvider
from app.services.risk_engine import RiskDecision, RiskEngine


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
    return RiskConfig(**defaults, _env_file=None)


# ── Mock provider + RiskEngine integration ────────────────────────────────────


class TestMockProviderFlow:
    """Full pipeline with MockAiProvider — always WAIT."""

    def test_mock_always_returns_wait(self):
        """Mock provider → RiskEngine → WAIT response."""
        engine = RiskEngine(_cfg())
        provider = MockAiProvider()

        import asyncio
        raw = asyncio.run(provider.decide({"symbol": "THYAO"}))

        from app.models.signal import SignalAction
        from app.services.risk_engine import RiskDecision

        decision = RiskDecision(
            action=SignalAction(raw["action"]),
            confidence=float(raw["confidence"]),
            reason=raw["reason"],
        )
        resp = engine.evaluate(_req(), decision)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False

    def test_mock_waits_even_with_strong_buy_signals(self):
        """Even with RSI=10, mock provider says WAIT."""
        engine = RiskEngine(_cfg())
        provider = MockAiProvider()

        import asyncio
        raw = asyncio.run(provider.decide({
            "symbol": "THYAO",
            "rsi": 10.0,
            "lastPrice": 100.0,
            "ema20": 80.0,
        }))

        from app.models.signal import SignalAction
        from app.services.risk_engine import RiskDecision

        decision = RiskDecision(
            action=SignalAction(raw["action"]),
            confidence=float(raw["confidence"]),
            reason=raw["reason"],
        )
        resp = engine.evaluate(
            _req(rsi=10.0, lastPrice=100.0, ema20=80.0, mode=SignalMode.LIVE),
            decision,
        )
        # RiskEngine may override reason for WAIT (confidence 0 < threshold 100)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False


# ── RiskEngine standalone tests (no provider dependency) ──────────────────────


class TestRiskEngineEdgeCases:
    """RiskEngine behaviour irrespective of which provider runs."""

    def test_unknown_symbol_overrides_any_decision(self):
        """GARAN not in allowed → WAIT, even if provider said BUY."""
        engine = RiskEngine(_cfg())
        req = _req(symbol="GARAN", mode=SignalMode.LIVE)
        decision = RiskDecision(
            action=SignalAction.BUY,
            confidence=95.0,
            reason="Dummy BUY",
            qty=10,
        )
        resp = engine.evaluate(req, decision)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False
        assert "not in the allowed list" in resp.reason

    def test_paper_mode_always_blocks(self):
        """PAPER mode → allow_order=False regardless of decision."""
        engine = RiskEngine(_cfg())
        req = _req(mode=SignalMode.PAPER)
        decision = RiskDecision(
            action=SignalAction.BUY,
            confidence=95.0,
            reason="Strong signal",
            qty=10,
        )
        resp = engine.evaluate(req, decision)
        assert resp.allow_order is False
        assert "PAPER mode" in resp.reason

    def test_low_confidence_buy_blocked(self):
        """BUY with confidence below threshold → allowed but order blocked."""
        engine = RiskEngine(_cfg(min_confidence_for_buy=75))
        req = _req(mode=SignalMode.LIVE)
        decision = RiskDecision(
            action=SignalAction.BUY,
            confidence=60.0,
            reason="Weak BUY",
            qty=5,
        )
        resp = engine.evaluate(req, decision)
        assert resp.allow_order is False
        assert "Confidence" in resp.reason

    def test_good_buy_passes_all_checks(self):
        """A valid BUY with high confidence in LIVE mode → allowed."""
        engine = RiskEngine(_cfg())
        req = _req(mode=SignalMode.LIVE)
        decision = RiskDecision(
            action=SignalAction.BUY,
            confidence=85.0,
            reason="Strong BUY",
            qty=5,
        )
        resp = engine.evaluate(req, decision)
        assert resp.action == SignalAction.BUY
        assert resp.allow_order is True
