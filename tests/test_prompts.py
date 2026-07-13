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
        assert (
            "provided structured data" in prompt.lower()
            or "use all provided" in prompt.lower()
        )

    def test_ignores_social_media_noise(self):
        prompt = get_trading_system_prompt()
        assert "social media" in prompt.lower()

    def test_uses_structured_contexts(self):
        """Prompt instructs to use newsContext and brokerFlowContext (smart
        money / AKD flow — live since the gateway /institutions integration).
        fundContext is intentionally NOT referenced — fund_scanner is still a
        placeholder; feeding the AI empty/UNKNOWN placeholders would be noise."""
        prompt = get_trading_system_prompt()
        assert "newscontext" in prompt.lower()
        assert "brokerflowcontext" in prompt.lower()
        assert "fundcontext" not in prompt.lower()

    def test_smart_money_rule(self):
        """Rule 14: smart-money STRONG_BUY confirms an asymmetric long;
        STRONG_SELL vetoes a BUY into strength (distribution trap)."""
        prompt = get_trading_system_prompt().lower()
        assert "smartmoneyflow" in prompt
        assert "strong_buy" in prompt
        assert "strong_sell" in prompt
        assert "distribution" in prompt
        assert "netsmartlot" in prompt

    def test_news_negativity_blocks_buy(self):
        """Rule 8: negative KAP/news blocks BUY."""
        prompt = get_trading_system_prompt()
        assert "negative" in prompt.lower() and "buy" in prompt.lower()
        assert "news context" in prompt.lower() or "newscontext" in prompt.lower()

    def test_ohlc_reliability_rule(self):
        """Rule 10: don't treat a flat ohlcReliable=false range as real
        price action."""
        prompt = get_trading_system_prompt()
        assert "ohlcreliable" in prompt.lower()

    def test_depth_reliability_rule(self):
        prompt = get_trading_system_prompt().lower()
        assert "depthreliable" in prompt
        assert "zero depth" in prompt or "unreliable" in prompt

    def test_quote_reliability_rule(self):
        """quoteReliable=false means lastPrice is a stale fallback, not a
        fresh live tick — the AI should be extra cautious."""
        prompt = get_trading_system_prompt().lower()
        assert "quotereliable" in prompt
        assert "pricesource" in prompt
        assert "stale" in prompt or "wait" in prompt

    def test_asymmetric_risk_reward_persona(self):
        """Persona: senior PM taking only asymmetric risk/reward trades."""
        prompt = get_trading_system_prompt().lower()
        assert "asymmetric" in prompt
        assert "portfolio manager" in prompt
        assert "hype" in prompt

    def test_red_lines_rule(self):
        """Rule 11: momentum/popularity alone never justifies BUY; theses
        must cite concrete payload signals."""
        prompt = get_trading_system_prompt().lower()
        assert "momentum or popularity alone" in prompt
        assert "two" in prompt and "independent" in prompt

    def test_bear_case_rule_and_output_field(self):
        """Rule 12: every BUY must include a bear_case field, and the
        OUTPUT FORMAT documents it."""
        prompt = get_trading_system_prompt()
        assert "bear_case" in prompt
        assert "refute" in prompt.lower() or "refutes" in prompt.lower()

    def test_allowed_symbols_rule(self):
        prompt = get_trading_system_prompt()
        assert "allowedsymbols" in prompt.lower()
        assert "orders are gated server-side" in prompt.lower()
        assert "must not change your analysis" in prompt.lower()

    def test_locked_symbols_rule(self):
        prompt = get_trading_system_prompt()
        assert "lockedsymbols" in prompt.lower()
        assert "locked long-term" in prompt.lower()

    def test_no_naked_sell_rule(self):
        prompt = get_trading_system_prompt()
        assert "botpositionqty" in prompt.lower()
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
