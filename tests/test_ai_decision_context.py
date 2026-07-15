import math

import pytest
from pydantic import ValidationError

from app.models.ai_decision_context import AiDecisionContext


def _context(**overrides):
    payload = {
        "symbol": "THYAO",
        "period": {"requested": "MIN5", "actual": "MIN5", "mismatch": False},
        "profile": "intraday",
        "evaluationPurpose": "TRADE_EVALUATION",
        "dataQuality": {"quoteAgeSec": 0, "quoteFresh": True, "quoteReliable": True},
        "price": {"last": 281.25},
        "market": {"barVolume": 0},
        "technical": {"rsi": 51.2, "indicatorBuyCount": 0},
    }
    payload.update(overrides)
    return payload


def test_ai_decision_context_serializes_compact_sections_and_keeps_zeros():
    context = AiDecisionContext.model_validate(
        _context(
            depth={"reliable": False},
            position={"botQty": 0, "lockedLongTerm": False},
            events={"kap": {"blockingRisk": False, "activeRiskCount": 0}},
        )
    )

    serialized = context.model_dump(exclude_none=True)

    assert serialized == {
        "schemaVersion": "ai-decision-context-v1",
        "symbol": "THYAO",
        "period": {"requested": "MIN5", "actual": "MIN5", "mismatch": False},
        "profile": "intraday",
        "evaluationPurpose": "TRADE_EVALUATION",
        "dataQuality": {"quoteAgeSec": 0.0, "quoteFresh": True, "quoteReliable": True},
        "price": {"last": 281.25},
        "market": {"barVolume": 0.0},
        "technical": {"rsi": 51.2, "indicatorBuyCount": 0},
        "depth": {"reliable": False},
        "position": {"botQty": 0.0, "lockedLongTerm": False},
        "events": {"kap": {"blockingRisk": False, "activeRiskCount": 0}},
    }


def test_ai_decision_context_rejects_forbidden_full_snapshot_and_nonfinite_values():
    with pytest.raises(ValidationError, match="agenticSteps"):
        AiDecisionContext.model_validate(_context(agenticSteps=[]))

    payload = _context()
    payload["price"]["last"] = math.inf
    with pytest.raises(ValidationError, match="finite_number"):
        AiDecisionContext.model_validate(payload)


def test_ai_decision_context_rejects_raw_urls_and_retains_compact_news_only():
    context = AiDecisionContext.model_validate(
        _context(
            events={
                "news": {
                    "items": [
                        {
                            "headline": "Earnings improve",
                            "summary": "Quarterly margin expanded.",
                            "sentiment": "POSITIVE",
                        }
                    ]
                }
            }
        )
    )
    assert context.events is not None
    assert context.events.news is not None
    assert context.events.news.items[0].headline == "Earnings improve"

    with pytest.raises(ValidationError, match="url"):
        AiDecisionContext.model_validate(
            _context(
                events={
                    "news": {
                        "items": [{"headline": "Earnings improve", "url": "https://x"}]
                    }
                }
            )
        )
