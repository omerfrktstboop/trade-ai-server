"""Central source of truth for strategy/prompt/schema version and config-hash
provenance stamps recorded on decisions, position lifecycles, and outcome rows.

Task 5 requires these values to come from exactly one place instead of being
hardcoded per-file, so that bumping a version is a one-line change here and
every consumer (evaluation persistence, PositionLifecycle, DecisionOutcome)
picks it up automatically. Nothing here changes AI prompt content, discovery
scoring, thresholds, or Trade Profile values - it only stamps existing
decisions with measurable version metadata.
"""

from __future__ import annotations

from app.config import AIProvider, settings
from app.services.effective_risk_config import EffectiveRiskConfig

# Bump these when the underlying strategy/prompt/context-schema actually
# changes. They intentionally do not affect any decision-making behavior.
STRATEGY_VERSION = "discovery-research-v2"
PROMPT_VERSION = "ai-decision-context-v2-prompt-1"
DECISION_CONTEXT_SCHEMA_VERSION = "ai-decision-context-v2"


def resolve_ai_provider_model() -> tuple[str, str | None]:
    """The configured AI backend identity, independent of a single decision's
    decisionSource (which AiDecision.provider already records separately)."""
    model = (
        settings.deepseek_model if settings.ai_provider == AIProvider.DEEPSEEK else None
    )
    return settings.ai_provider.value, model


def resolve_config_hash(limits: EffectiveRiskConfig | None) -> str | None:
    """Fingerprint of the effective config that produced a decision.

    Combines the system config version with the environment fingerprint so
    the resulting string changes whenever either input changes.
    """
    if limits is None:
        return None
    return f"{limits.system_config_version}:{limits.environment_config_fingerprint}"


def resolve_profile_code(limits: EffectiveRiskConfig | None) -> str | None:
    if limits is None:
        return None
    return limits.trade_profile_code
