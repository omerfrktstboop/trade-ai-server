"""AI provider abstraction layer.

Each provider implements ``async def decide(payload) -> dict`` and returns
a JSON-serializable decision dictionary. The provider factory selects the
active implementation based on ``AI_PROVIDER`` in the app config.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from app.config import AIProvider, settings

logger = logging.getLogger(__name__)


# ── Abstract base ─────────────────────────────────────────────────────────────


class AiProvider(ABC):
    """Interface every AI provider must implement."""

    @abstractmethod
    async def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Produce a trading decision from the signal payload.

        Parameters:
            payload: Serialized SignalRequest fields (OHLC, indicators, …).

        Returns:
            A dict with at minimum:
            - ``action``: ``"BUY"``, ``"SELL"``, or ``"WAIT"``
            - ``confidence``: float 0-100
            - ``reason``: human-readable explanation
            Optional: ``qty``, ``stop_loss``, ``target_price``, ``risk_score``
        """
        ...


# ── Mock provider ─────────────────────────────────────────────────────────────


class MockAiProvider(AiProvider):
    """Always returns WAIT — safe no-op for testing and development."""

    async def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        logger.debug("MockAiProvider.decide called — returning WAIT")
        return {
            "action": "WAIT",
            "confidence": 0.0,
            "reason": "Mock provider — always WAIT",
        }


# ── DeepSeek provider (skeleton) ──────────────────────────────────────────────


class DeepSeekProvider(AiProvider):
    """DeepSeek API provider — skeleton, real implementation in a later task."""

    def __init__(
        self,
        api_key: str = "",
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com/v1",
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self._session: Any = None  # aiohttp session, created lazily

    async def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        """⚠️ Not yet implemented — always falls back to WAIT.

        Full implementation will:
        1. Build a system + user prompt from the payload.
        2. Call ``POST {base_url}/chat/completions`` with the model.
        3. Parse the JSON response into the standard decision dict.
        """
        logger.warning("DeepSeekProvider.decide called but not implemented")
        return {
            "action": "WAIT",
            "confidence": 0.0,
            "reason": "DeepSeek provider — skeleton (not implemented yet)",
        }


# ── Provider factory ──────────────────────────────────────────────────────────


def get_provider(name: str | AIProvider | None = None) -> AiProvider:
    """Return a configured provider instance for the given name.

    Parameters:
        name: One of ``"mock"``, ``"deepseek"``, or None (uses ``settings.ai_provider``).

    Returns:
        An AiProvider instance ready for ``await provider.decide(payload)``.

    Raises:
        ValueError: Unknown provider name.
    """
    resolved = (name or settings.ai_provider).lower() if isinstance(name, str) else name

    if isinstance(name, AIProvider):
        resolved = name.value

    if resolved in ("mock",):
        return MockAiProvider()

    if resolved in ("deepseek",):
        return DeepSeekProvider(
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
        )

    raise ValueError(
        f"Unknown AI_PROVIDER: {resolved!r}. "
        f"Supported: mock, deepseek"
    )


# ── Module-level default ──────────────────────────────────────────────────────

_default_provider: AiProvider | None = None


def get_default_provider() -> AiProvider:
    """Return the singleton default provider (lazy-init from settings)."""
    global _default_provider
    if _default_provider is None:
        _default_provider = get_provider()
    return _default_provider
