"""Integration tests for the signal evaluate endpoint flow.

Tests the full chain:  SignalRequest → AiProvider → RiskDecision → RiskEngine → SignalResponse

With ``AI_PROVIDER=mock`` (default) the provider always returns WAIT,
so the endpoint is always safe regardless of the input.
"""

from __future__ import annotations


from app.core.risk_config import RiskConfig
from app.models.signal import SignalAction, SignalMode, SignalRequest, EntryRange
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
        disable_trading_after="23:59",
        timezone="Etc/GMT+12",
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

        raw = asyncio.run(
            provider.decide(
                {
                    "symbol": "THYAO",
                    "rsi": 10.0,
                    "lastPrice": 100.0,
                    "ema20": 80.0,
                }
            )
        )

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
            entry_range=EntryRange(min=95.0, max=102.0),
            stop_loss=93.0,
            target_price=110.0,
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
            entry_range=EntryRange(min=95.0, max=102.0),
            stop_loss=93.0,
            target_price=110.0,
        )
        resp = engine.evaluate(req, decision)
        assert resp.action == SignalAction.BUY
        assert resp.allow_order is True


# ── _dict_to_risk_decision defensive parsing ────────────────────────────────


class TestDictToRiskDecisionDefense:
    """_dict_to_risk_decision must never raise, regardless of AI output."""

    def test_invalid_action_hold_fallback_wait(self):
        """raw action 'HOLD' → WAIT with fallback reason."""
        from app.routers.signal import _dict_to_risk_decision

        decision = _dict_to_risk_decision(
            {"action": "HOLD", "confidence": 80, "reason": "hold signal"},
            _req(),
        )
        assert decision.action == SignalAction.WAIT
        assert "Invalid AI action" in decision.reason
        assert "fallback WAIT" in decision.reason
        # Original reason preserved
        assert "hold signal" in decision.reason

    def test_null_action_fallback_wait(self):
        """raw action None/null → WAIT."""
        from app.routers.signal import _dict_to_risk_decision

        decision = _dict_to_risk_decision(
            {"action": None, "confidence": 80, "reason": "null action"},
            _req(),
        )
        assert decision.action == SignalAction.WAIT

    def test_empty_action_string_fallback_wait(self):
        """raw action '' → WAIT."""
        from app.routers.signal import _dict_to_risk_decision

        decision = _dict_to_risk_decision(
            {"action": "", "confidence": 80, "reason": "empty"},
            _req(),
        )
        assert decision.action == SignalAction.WAIT

    def test_missing_action_field_fallback_wait(self):
        """No 'action' key → WAIT (default)."""
        from app.routers.signal import _dict_to_risk_decision

        decision = _dict_to_risk_decision(
            {"confidence": 80, "reason": "no action"},
            _req(),
        )
        assert decision.action == SignalAction.WAIT
        # No fallback message for missing field (it's just the default)
        assert "Invalid AI action" not in decision.reason

    def test_invalid_confidence_string_defaults_zero(self):
        """confidence='high' → 0.0."""
        from app.routers.signal import _dict_to_risk_decision

        decision = _dict_to_risk_decision(
            {"action": "BUY", "confidence": "high", "reason": "string conf"},
            _req(),
        )
        assert decision.action == SignalAction.BUY  # action itself is valid
        assert decision.confidence == 0.0

    def test_non_numeric_qty_defaults_zero(self):
        """qty='many' → 0.0."""
        from app.routers.signal import _dict_to_risk_decision

        decision = _dict_to_risk_decision(
            {"action": "SELL", "confidence": 70, "qty": "many", "reason": "bad qty"},
            _req(),
        )
        assert decision.qty == 0.0

    def test_garbage_stop_loss_defaults_none(self):
        """stop_loss='n/a' → None."""
        from app.routers.signal import _dict_to_risk_decision

        decision = _dict_to_risk_decision(
            {"action": "BUY", "confidence": 85, "stop_loss": "n/a", "reason": "bad sl"},
            _req(),
        )
        assert decision.stop_loss is None

    def test_completely_empty_dict(self):
        """Empty dict → WAIT with all defaults."""
        from app.routers.signal import _dict_to_risk_decision

        decision = _dict_to_risk_decision({}, _req())
        assert decision.action == SignalAction.WAIT
        assert decision.confidence == 0.0
        assert decision.risk_score == 0.0
        assert "Provider returned no reason" in decision.reason

    def test_valid_buy_still_works(self):
        """Valid BUY with all fields still parses correctly."""
        from app.routers.signal import _dict_to_risk_decision

        decision = _dict_to_risk_decision(
            {
                "action": "BUY",
                "confidence": 85,
                "risk_score": 10,
                "reason": "strong signal",
                "qty": 10,
                "entry_range": {"min": 95, "max": 102},
                "stop_loss": 93,
                "target_price": 110,
            },
            _req(),
        )
        assert decision.action == SignalAction.BUY
        assert decision.confidence == 85.0
        assert decision.risk_score == 10.0
        assert decision.qty == 10.0
        assert decision.stop_loss == 93.0
        assert decision.target_price == 110.0
        assert decision.reason == "strong signal"
        assert "Invalid AI action" not in decision.reason

    def test_valid_sell_still_works(self):
        """Valid SELL still parses correctly."""
        from app.routers.signal import _dict_to_risk_decision

        decision = _dict_to_risk_decision(
            {"action": "SELL", "confidence": 90, "reason": "overbought"},
            _req(),
        )
        assert decision.action == SignalAction.SELL
        assert decision.confidence == 90.0

    def test_pipeline_never_500_with_garbage_input(self):
        """Full pipeline: garbage AI dict → WAIT, no exception."""
        engine = RiskEngine(_cfg())

        from app.routers.signal import _dict_to_risk_decision

        garbage = {
            "action": "NONE",
            "confidence": "what?",
            "reason": 42,
            "qty": [1, 2, 3],
            "stop_loss": "nope",
            "target_price": None,
        }
        decision = _dict_to_risk_decision(garbage, _req())
        resp = engine.evaluate(_req(), decision)

        # Must not raise, must return WAIT
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False


class TestNormalizeDecisionToRiskDecisionPipeline:
    """Full pipeline: raw provider dict -> _normalize_decision -> _dict_to_risk_decision.

    Regression coverage for a bug where _normalize_decision silently dropped
    entryRange/stopLoss/targetPrice before _dict_to_risk_decision ever saw
    them, even though _dict_to_risk_decision was already written to parse
    both camelCase and snake_case correctly.
    """

    def test_camel_case_buy_response_keeps_entry_range_stop_loss_target_price(self):
        from app.services.ai_provider import _normalize_decision
        from app.routers.signal import _dict_to_risk_decision

        raw = {
            "action": "BUY",
            "confidence": 82,
            "qty": 1,
            "entryRange": {"min": 100, "max": 101},
            "stopLoss": 98,
            "targetPrice": 106,
            "risk_score": 20,
            "reason": "test",
        }

        normalized = _normalize_decision(raw)
        decision = _dict_to_risk_decision(normalized, _req())

        assert decision.action == SignalAction.BUY
        assert decision.entry_range is not None
        assert decision.entry_range.min == 100.0
        assert decision.entry_range.max == 101.0
        assert decision.stop_loss == 98.0
        assert decision.target_price == 106.0

    def test_documented_snake_case_buy_response_keeps_entry_range(self):
        """The system prompt asks the model for snake_case — that must also work."""
        from app.services.ai_provider import _normalize_decision
        from app.routers.signal import _dict_to_risk_decision

        raw = {
            "action": "BUY",
            "confidence": 82,
            "entry_range": {"min": 100, "max": 101},
            "stop_loss": 98,
            "target_price": 106,
            "reason": "test",
        }

        normalized = _normalize_decision(raw)
        decision = _dict_to_risk_decision(normalized, _req())

        assert decision.entry_range is not None
        assert decision.entry_range.min == 100.0
        assert decision.entry_range.max == 101.0
        assert decision.stop_loss == 98.0
        assert decision.target_price == 106.0


class TestOhlcReliableFlag:
    """ohlcReliable must survive parsing and reach the AI-facing payload —
    Matriks sets it false when open/high/low are just lastPrice repeated
    (no real bar data yet), and the AI needs to know not to trust that flat
    range as a real price range."""

    def test_signal_request_accepts_ohlc_reliable_alias(self):
        req = _req(ohlcReliable=False)
        assert req.ohlc_reliable is False

    def test_signal_request_defaults_to_none(self):
        req = _req()
        assert req.ohlc_reliable is None

    def test_build_payload_includes_ohlc_reliable(self):
        from app.routers.signal import _build_payload

        req = _req(ohlcReliable=False)
        payload = _build_payload(req)
        assert payload["ohlcReliable"] is False

    def test_build_payload_includes_none_when_not_provided(self):
        from app.routers.signal import _build_payload

        req = _req()
        payload = _build_payload(req)
        assert payload["ohlcReliable"] is None
