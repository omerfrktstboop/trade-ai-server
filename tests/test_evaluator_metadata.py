"""Decision persistence labels must distinguish LLM calls from safe gates."""

from __future__ import annotations

from app.config import AIProvider
from app.services import evaluator


def test_llm_decision_records_configured_deepseek_model(monkeypatch):
    monkeypatch.setattr(evaluator.settings, "ai_provider", AIProvider.DEEPSEEK)
    monkeypatch.setattr(evaluator.settings, "deepseek_model", "deepseek-chat")

    assert evaluator._decision_persistence_metadata({"decisionSource": "llm"}) == (
        "deepseek",
        "deepseek-chat",
    )


def test_preflight_decision_does_not_claim_a_model_was_called():
    assert evaluator._decision_persistence_metadata(
        {"decisionSource": "preflight-gate"}
    ) == ("preflight-gate", None)
