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
    assert "at most the three items" in prompt
    assert "headline" in prompt
    assert "summary" in prompt
    assert "sentiment" in prompt
    assert "untrusted" in prompt


def test_prompt_requires_json_decision_format():
    prompt = get_trading_system_prompt()
    assert "JSON ONLY" in prompt
    assert '"action":' in prompt
    assert "entry_range" in prompt
