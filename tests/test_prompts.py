"""Unit tests for app.core.prompts — trading system prompt integrity."""

from __future__ import annotations

from app.core.prompts import get_trading_system_prompt


class TestTradingSystemPrompt:
    """Validate the trading system prompt used by AI providers."""

    def test_returns_non_empty_string(self):
        prompt = get_trading_system_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 200  # Must be substantial

    def test_contains_persona_hedge_fund(self):
        prompt = get_trading_system_prompt()
        assert "hedge-fund" in prompt.lower() or "analyst" in prompt.lower()

    def test_contains_data_driven_rule(self):
        prompt = get_trading_system_prompt()
        assert "provided structured data" in prompt.lower() or "use all provided" in prompt.lower()

    def test_ignores_social_media_noise(self):
        prompt = get_trading_system_prompt()
        assert "social media" in prompt.lower()

    def test_uses_structured_contexts(self):
        """Prompt instructs to use newsContext, fundContext, brokerFlowContext."""
        prompt = get_trading_system_prompt()
        assert "newscontext" in prompt.lower()
        assert "fundcontext" in prompt.lower()
        assert "brokerflowcontext" in prompt.lower()

    def test_news_negativity_blocks_buy(self):
        """Rule 8: negative KAP/news blocks BUY."""
        prompt = get_trading_system_prompt()
        assert "negative" in prompt.lower() and "buy" in prompt.lower()
        assert "news context" in prompt.lower() or "newscontext" in prompt.lower()

    def test_fund_broker_confidence_boost(self):
        """Rule 9: fund + broker positivity adds confidence score."""
        prompt = get_trading_system_prompt()
        assert "confidence" in prompt.lower()
        assert "10-20" in prompt or "10‑20" in prompt

    def test_allowed_symbols_rule(self):
        prompt = get_trading_system_prompt()
        assert "allowedsymbols" in prompt.lower()
        assert "symbol not in allowed" in prompt.lower()

    def test_locked_symbols_rule(self):
        prompt = get_trading_system_prompt()
        assert "lockedsymbols" in prompt.lower()
        assert "locked long-term" in prompt.lower()

    def test_no_naked_sell_rule(self):
        prompt = get_trading_system_prompt()
        assert "zero position" in prompt.lower() or "no position" in prompt.lower()
        assert "short selling" in prompt.lower()

    def test_buy_requires_entry_stop_target(self):
        prompt = get_trading_system_prompt()
        assert "entry_range" in prompt
        assert "stop_loss" in prompt
        assert "target_price" in prompt

    def test_insufficient_data_rule(self):
        prompt = get_trading_system_prompt()
        assert "insufficient" in prompt.lower() or "missing" in prompt.lower()

    def test_wait_is_safe_default(self):
        prompt = get_trading_system_prompt()
        assert "WAIT is the safe default" in prompt or "safe default" in prompt

    def test_json_only_output_requirement(self):
        prompt = get_trading_system_prompt()
        assert "JSON ONLY" in prompt or "JSON only" in prompt
        assert "no preamble" in prompt.lower() or "no markdown" in prompt.lower()

    def test_rejection_of_non_json_responses(self):
        prompt = get_trading_system_prompt()
        assert "rejected" in prompt.lower()

    def test_action_field_documented(self):
        prompt = get_trading_system_prompt()
        assert '"action":' in prompt
        assert "BUY" in prompt
        assert "SELL" in prompt
        assert "WAIT" in prompt

    def test_confidence_range_documented(self):
        prompt = get_trading_system_prompt()
        assert "confidence" in prompt.lower()
        assert "0-100" in prompt

    def test_indicator_reference_present(self):
        prompt = get_trading_system_prompt()
        assert "RSI" in prompt
        assert "EMA" in prompt
        assert "MACD" in prompt
        assert "Volume" in prompt

    def test_bollinger_bands_mentioned(self):
        prompt = get_trading_system_prompt()
        assert "Bollinger" in prompt

    def test_idempotent_call(self):
        """Calling the function twice returns the same content."""
        p1 = get_trading_system_prompt()
        p2 = get_trading_system_prompt()
        assert p1 == p2
