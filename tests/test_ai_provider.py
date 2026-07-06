"""Unit tests for AI providers."""

from __future__ import annotations

import pytest

from app.services.ai_provider import (
    AiProvider,
    DeepSeekProvider,
    MockAiProvider,
    get_provider,
)


class TestMockProvider:
    """Mock provider always returns WAIT."""

    @pytest.mark.asyncio
    async def test_always_returns_wait(self):
        provider = MockAiProvider()
        result = await provider.decide({"symbol": "THYAO"})

        assert result["action"] == "WAIT"
        assert result["confidence"] == 0.0
        assert "Mock" in result["reason"]

    @pytest.mark.asyncio
    async def test_ignores_input_always_safe(self):
        """Even with a bullish payload, mock returns WAIT."""
        provider = MockAiProvider()
        result = await provider.decide({
            "symbol": "THYAO",
            "rsi": 20.0,
            "lastPrice": 100.0,
            "ema20": 80.0,
        })

        assert result["action"] == "WAIT"


class TestDeepSeekSkeleton:
    """DeepSeek is a skeleton — falls back to WAIT."""

    @pytest.mark.asyncio
    async def test_returns_wait_skeleton(self):
        provider = DeepSeekProvider(api_key="sk-test")
        result = await provider.decide({"symbol": "THYAO"})

        assert result["action"] == "WAIT"
        assert result["confidence"] == 0.0
        assert "skeleton" in result["reason"]

    @pytest.mark.asyncio
    async def test_accepts_init_params(self):
        provider = DeepSeekProvider(
            api_key="sk-abc",
            model="deepseek-chat",
            base_url="https://custom.api/v1",
        )
        assert provider.api_key == "sk-abc"
        assert provider.model == "deepseek-chat"
        assert provider.base_url == "https://custom.api/v1"


class TestFactory:
    """Provider factory returns correct implementations."""

    def test_factory_mock(self):
        provider = get_provider("mock")
        assert isinstance(provider, MockAiProvider)

    def test_factory_deepseek(self):
        provider = get_provider("deepseek")
        assert isinstance(provider, DeepSeekProvider)

    def test_factory_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown AI_PROVIDER"):
            get_provider("nonexistent")

    def test_factory_case_insensitive(self):
        provider = get_provider("MOCK")
        assert isinstance(provider, MockAiProvider)


class TestAiProviderInterface:
    """Abstract base cannot be instantiated directly."""

    def test_abstract_cannot_instantiate(self):
        with pytest.raises(TypeError):
            AiProvider()  # type: ignore[abstract]
