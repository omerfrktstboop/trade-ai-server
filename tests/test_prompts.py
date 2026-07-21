"""Unit tests for the compact trading system prompt."""

from app.core.prompts import get_trading_system_prompt


def test_trading_prompt_documents_compact_context_contract():
    prompt = get_trading_system_prompt()

    for field in (
        "schemaVersion",
        "period.requested",
        "period.actual",
        "period.mismatch",
        "evaluationPurpose",
        "price.last",
        "price.open",
        "price.high",
        "price.low",
        "events.news.items",
        "events.kap",
        "events.brokerFlow",
        "position.botQty",
    ):
        assert field in prompt


def test_trading_prompt_rejects_legacy_payload_expectations():
    prompt = get_trading_system_prompt()

    for field in (
        "newsContext",
        "technicalFeatures",
        "agenticSteps",
        "allowedSymbols",
        "declinedSymbols",
        "lockedSymbols",
        "botPositionQty",
        "depthContext",
        "lastPrice",
        "fundamentalsContext",
    ):
        assert field not in prompt


def test_research_is_analytical_only():
    prompt = get_trading_system_prompt()
    assert "RESEARCH_DISCOVERY" in prompt
    assert "never grants order authority" in prompt
    assert "research_score" in prompt


def test_news_is_limited_and_untrusted():
    prompt = get_trading_system_prompt()
    assert "At most two compact news items" in prompt
    assert "headline" in prompt
    assert "summary" in prompt
    assert "sentiment" in prompt
    assert "untrusted" in prompt


def test_prompt_requires_json_decision_format():
    prompt = get_trading_system_prompt()
    assert "JSON ONLY" in prompt
    assert '"action":' in prompt
    assert "entry_range" in prompt
    assert "target_allocation_pct" in prompt
    assert "opportunity_score" in prompt


def test_ai_recommends_allocation_but_never_quantity_or_tl_amount():
    for tools_enabled in (False, True):
        prompt = get_trading_system_prompt(tools_enabled=tools_enabled)
        normalized = " ".join(prompt.split())
        assert "desired post-trade value" in normalized
        assert "operator-defined total bot capital budget" in normalized
        assert "order quantity, lot count, or a TL amount" in normalized


def test_prompt_contains_compact_risk_and_position_rules():
    prompt = get_trading_system_prompt()
    normalized = " ".join(prompt.split())

    assert "technical.natr" in prompt
    assert "technical.atr" in prompt
    assert "1.5 x technical.natr" in prompt
    assert "1%" in prompt and "10%" in prompt
    assert "target distance must be at least 1.5 times" in prompt
    assert "critical volatility data is unavailable" in prompt
    assert "strongly supported WAIT may have high confidence" in prompt
    assert "low or medium confidence" in normalized
    assert "risk_score`` for every action" in prompt
    for risk_input in (
        "volatility",
        "spread",
        "depth reliability",
        "data age",
        "news",
        "KAP",
    ):
        assert risk_input in prompt
    for position_rule in (
        "position.botQty",
        "TAKE PROFIT",
        "CUT LOSS",
        "HOLD",
        "position.lockedLongTerm",
        "materially strengthened",
    ):
        assert position_rule in prompt
