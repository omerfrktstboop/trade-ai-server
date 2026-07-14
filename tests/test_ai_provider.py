"""Unit tests for DeepSeek provider — async API calls, parsing, fallback."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ai_provider import (
    DeepSeekProvider,
    MockAiProvider,
    _extract_json,
    _normalize_decision,
    get_provider,
)

COMPACT_CONTEXT = {
    "schemaVersion": "ai-decision-context-v1",
    "symbol": "THYAO",
    "period": {"requested": "MIN5", "actual": "MIN5", "mismatch": False},
    "evaluationPurpose": "TRADE_EVALUATION",
    "dataQuality": {},
    "price": {"last": 100.0},
    "market": {},
    "technical": {},
}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _provider(**kwargs) -> DeepSeekProvider:
    defaults = {
        "api_key": "sk-test-key",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "timeout": 10.0,
    }
    defaults.update(kwargs)
    return DeepSeekProvider(**defaults)


def _fake_resp(status: int = 200, content: str = "", json_body: dict | None = None):
    """Build a mock aiohttp response."""
    resp = AsyncMock()
    resp.status = status

    if json_body is not None:
        resp.json = AsyncMock(return_value=json_body)
        resp.text = AsyncMock(return_value=json.dumps(json_body))
    else:
        resp.text = AsyncMock(return_value=content)
        # json() will try to parse content — if it's JSON, parse it;
        # if it's plain text, raise
        try:
            parsed = json.loads(content)
            resp.json = AsyncMock(return_value=parsed)
        except json.JSONDecodeError:
            resp.json = AsyncMock(side_effect=ValueError("not json"))

    return resp


def _mock_session(resp=None):
    """Return a mock ClientSession whose post() returns the given response."""

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    # session.post() returns a context manager that yields the response
    ctx_mgr = AsyncMock()
    ctx_mgr.__aenter__ = AsyncMock(
        return_value=resp
        if resp
        else _fake_resp(
            status=200,
            json_body={
                "choices": [
                    {
                        "message": {
                            "content": '{"action": "BUY", "confidence": 85, "reason": "RSI oversold"}'
                        }
                    }
                ]
            },
        )
    )
    ctx_mgr.__aexit__ = AsyncMock(return_value=None)

    session.post = MagicMock(return_value=ctx_mgr)

    return session


# ── _extract_json tests ───────────────────────────────────────────────────────


class TestExtractJson:
    def test_direct_json(self):
        result = _extract_json('{"action": "BUY", "confidence": 80}')
        assert result == {"action": "BUY", "confidence": 80}

    def test_json_block(self):
        result = _extract_json('```json\n{"action": "SELL", "confidence": 90}\n```')
        assert result == {"action": "SELL", "confidence": 90}

    def test_code_block_no_lang(self):
        result = _extract_json('```\n{"action": "WAIT", "confidence": 50}\n```')
        assert result == {"action": "WAIT", "confidence": 50}

    def test_embedded_braces(self):
        """Text with JSON nested inside."""
        result = _extract_json(
            'Here is my decision: {"action": "BUY", "confidence": 75}'
        )
        assert result == {"action": "BUY", "confidence": 75}

    def test_nested_json(self):
        result = _extract_json('{"action": "WAIT", "confidence": 50, "meta": {"v": 1}}')
        assert result == {"action": "WAIT", "confidence": 50, "meta": {"v": 1}}

    def test_garbled_text_returns_none(self):
        result = _extract_json("Just some random text, no JSON here")
        assert result is None

    def test_incomplete_json_returns_none(self):
        result = _extract_json('{"action": "BUY"')
        assert result is None


# ── _normalize_decision tests ─────────────────────────────────────────────────


class TestNormalizeDecision:
    def test_valid_decision_passes_through(self):
        result = _normalize_decision(
            {"action": "BUY", "confidence": 80.0, "reason": "strong signal"}
        )
        assert result["action"] == "BUY"
        assert result["confidence"] == 80.0

    def test_missing_action_defaults_wait(self):
        result = _normalize_decision({"confidence": 70, "reason": "test"})
        assert result["action"] == "WAIT"

    def test_unknown_action_defaults_wait(self):
        result = _normalize_decision({"action": "HODL", "confidence": 99})
        assert result["action"] == "WAIT"

    def test_case_normalized(self):
        result = _normalize_decision({"action": "buy", "confidence": 80})
        assert result["action"] == "BUY"

    def test_confidence_clamped(self):
        result = _normalize_decision({"action": "WAIT", "confidence": 150})
        assert result["confidence"] == 100.0

        result = _normalize_decision({"action": "WAIT", "confidence": -10})
        assert result["confidence"] == 0.0

    def test_non_numeric_confidence_defaults_50(self):
        result = _normalize_decision({"action": "WAIT", "confidence": "high"})
        assert result["confidence"] == 50.0

    def test_optional_fields(self):
        result = _normalize_decision(
            {
                "action": "BUY",
                "confidence": 80,
                "qty": 5,
                "stop_loss": 95.5,
                "target_price": 110.0,
            }
        )
        assert "qty" not in result
        assert result["_audit_raw_response"]["qty"] == 5
        assert result["stop_loss"] == 95.5
        assert result["target_price"] == 110.0

    def test_invalid_optional_fields_skipped(self):
        result = _normalize_decision(
            {"action": "WAIT", "confidence": 50, "qty": "many", "stop_loss": "n/a"}
        )
        assert "qty" not in result
        assert "stop_loss" not in result

    def test_camel_case_stop_loss_and_target_price_preserved(self):
        """DeepSeek sometimes replies camelCase (matching the rest of the API's
        JSON convention) instead of the snake_case documented in the system
        prompt — both must survive normalization."""
        result = _normalize_decision(
            {
                "action": "BUY",
                "confidence": 80,
                "stopLoss": 98.0,
                "targetPrice": 106.0,
            }
        )
        assert result["stop_loss"] == 98.0
        assert result["target_price"] == 106.0

    def test_entry_range_nested_camel_case_preserved(self):
        result = _normalize_decision(
            {
                "action": "BUY",
                "confidence": 80,
                "entryRange": {"min": 100, "max": 101},
            }
        )
        assert result["entryRange"] == {"min": 100, "max": 101}

    def test_entry_range_snake_case_preserved(self):
        result = _normalize_decision(
            {
                "action": "BUY",
                "confidence": 80,
                "entry_range": {"min": 100, "max": 101},
            }
        )
        assert result["entry_range"] == {"min": 100, "max": 101}

    def test_entry_range_absent_when_not_provided(self):
        result = _normalize_decision({"action": "WAIT", "confidence": 50})
        assert "entry_range" not in result
        assert "entryRange" not in result

    def test_bear_case_snake_case_preserved(self):
        """bear_case must survive normalization — it's persisted in
        raw_response and shown in the admin log detail view. The whitelist
        would otherwise silently drop it."""
        result = _normalize_decision(
            {
                "action": "BUY",
                "confidence": 80,
                "bear_case": "Thesis fails if RSI breaks below 40 with volume.",
            }
        )
        assert result["bear_case"] == "Thesis fails if RSI breaks below 40 with volume."

    def test_bear_case_camel_case_preserved(self):
        result = _normalize_decision(
            {
                "action": "BUY",
                "confidence": 80,
                "bearCase": "Fails on negative KAP filing.",
            }
        )
        assert result["bear_case"] == "Fails on negative KAP filing."

    def test_bear_case_absent_when_not_provided(self):
        result = _normalize_decision({"action": "WAIT", "confidence": 50})
        assert "bear_case" not in result


# ── DeepSeek provider — async decide tests ─────────────────────────────────────


class TestDeepSeekDecide:
    @pytest.mark.asyncio
    async def test_successful_buy_decision(self):
        """Happy path: valid JSON BUY response."""
        provider = _provider()
        resp = _fake_resp(
            status=200,
            json_body={
                "choices": [
                    {
                        "message": {
                            "content": '{"action": "BUY", "confidence": 85.5, "reason": "RSI oversold at 22"}'
                        }
                    }
                ]
            },
        )

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = _mock_session(resp)
            result = await provider.decide(COMPACT_CONTEXT | {"technical": {"rsi": 22.0}})

        assert result["action"] == "BUY"
        assert result["confidence"] == 85.5
        assert "oversold" in result["reason"]

    @pytest.mark.asyncio
    async def test_successful_sell_decision(self):
        """Happy path: valid JSON SELL response."""
        provider = _provider()
        resp = _fake_resp(
            status=200,
            json_body={
                "choices": [
                    {
                        "message": {
                            "content": '{"action": "SELL", "confidence": 90, "reason": "RSI overbought", "qty": 10}'
                        }
                    }
                ]
            },
        )

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = _mock_session(resp)
            result = await provider.decide(COMPACT_CONTEXT | {"symbol": "AKBNK", "technical": {"rsi": 78.0}})

        assert result["action"] == "SELL"
        assert result["confidence"] == 90.0
        assert result.get("qty") is None
        assert result["_audit_raw_response"]["qty"] == 10

    @pytest.mark.asyncio
    async def test_json_inside_code_block(self):
        """Model wraps response in ```json block."""
        provider = _provider()
        content = '```json\n{"action": "WAIT", "confidence": 60, "reason": "neutral RSI"}\n```'
        resp = _fake_resp(
            status=200,
            json_body={"choices": [{"message": {"content": content}}]},
        )

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = _mock_session(resp)
            result = await provider.decide(COMPACT_CONTEXT)

        assert result["action"] == "WAIT"
        assert result["confidence"] == 60.0

    @pytest.mark.asyncio
    async def test_http_error_fallback(self):
        """Non-200 status → WAIT fallback."""
        provider = _provider()
        resp = _fake_resp(status=401, content='{"error": "unauthorized"}')

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = _mock_session(resp)
            result = await provider.decide(COMPACT_CONTEXT)

        assert result["action"] == "WAIT"
        assert result["confidence"] == 0.0
        assert "API error 401" in result["reason"]

    @pytest.mark.asyncio
    async def test_network_error_fallback(self):
        """aiohttp.ClientError → WAIT fallback."""
        provider = _provider()
        session = _mock_session()
        session.post.side_effect = __import__("aiohttp").ClientError(
            "connection refused"
        )

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = session
            result = await provider.decide(COMPACT_CONTEXT)

        assert result["action"] == "WAIT"
        assert "connection refused" in result["reason"]

    @pytest.mark.asyncio
    async def test_timeout_error_fallback(self):
        """Timeout → WAIT fallback."""
        provider = _provider()
        session = _mock_session()
        session.post.side_effect = __import__("asyncio").TimeoutError()

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = session
            result = await provider.decide(COMPACT_CONTEXT)

        assert result["action"] == "WAIT"
        assert (
            "timed out" in result["reason"].lower()
            or "timeout" in result["reason"].lower()
        )

    @pytest.mark.asyncio
    async def test_unparseable_response_fallback(self):
        """Model returns garbled text → WAIT fallback."""
        provider = _provider()
        resp = _fake_resp(
            status=200,
            json_body={
                "choices": [
                    {"message": {"content": "I think you should buy this stock!"}}
                ]
            },
        )

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = _mock_session(resp)
            result = await provider.decide(COMPACT_CONTEXT)

        assert result["action"] == "WAIT"
        assert "Could not parse" in result["reason"]

    @pytest.mark.asyncio
    async def test_empty_choices_fallback(self):
        """No choices in response → WAIT fallback."""
        provider = _provider()
        resp = _fake_resp(status=200, json_body={"choices": []})

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = _mock_session(resp)
            result = await provider.decide(COMPACT_CONTEXT)

        assert result["action"] == "WAIT"
        assert "Empty response" in result["reason"]

    @pytest.mark.asyncio
    async def test_empty_content_fallback(self):
        """Empty content field → WAIT fallback."""
        provider = _provider()
        resp = _fake_resp(
            status=200,
            json_body={"choices": [{"message": {"content": ""}}]},
        )

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = _mock_session(resp)
            result = await provider.decide(COMPACT_CONTEXT)

        assert result["action"] == "WAIT"
        assert "Empty content" in result["reason"]

    @pytest.mark.asyncio
    async def test_payload_includes_compact_context_only(self):
        """Verify that the payload is serialized and sent correctly."""
        provider = _provider()
        resp = _fake_resp(
            status=200,
            json_body={
                "choices": [
                    {
                        "message": {
                            "content": '{"action": "WAIT", "confidence": 50, "reason": "test"}'
                        }
                    }
                ]
            },
        )

        session = _mock_session(resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = session
            await provider.decide(
                {
                    "schemaVersion": "ai-decision-context-v1",
                    "symbol": "TUPRS",
                    "period": {"requested": "MIN5", "actual": "MIN5", "mismatch": False},
                    "evaluationPurpose": "TRADE_EVALUATION",
                    "dataQuality": {"quoteReliable": True},
                    "price": {"last": 205.0},
                    "market": {},
                    "technical": {"rsi": 45.0, "ema20": 200.0},
                }
            )

        # Check that the request was made with proper body
        call_args = session.post.call_args
        assert call_args is not None

        url = call_args[0][0] if call_args[0] else ""
        assert "/chat/completions" in url

        json_body = call_args.kwargs.get("json", {})
        assert json_body["model"] == "deepseek-chat"
        assert len(json_body["messages"]) == 2
        assert json_body["messages"][0]["role"] == "system"
        assert json_body["messages"][1]["role"] == "user"
        assert "TUPRS" in json_body["messages"][1]["content"]
        assert '"price": {' in json_body["messages"][1]["content"]
        assert "lastPrice" not in json_body["messages"][1]["content"]


# ── Factory tests for new config ──────────────────────────────────────────────


class TestFactoryWithConfig:
    def test_deepseek_factory_uses_config_values(self):
        provider = get_provider("deepseek")
        assert isinstance(provider, DeepSeekProvider)
        assert provider.base_url == "https://api.deepseek.com/v1"
        assert provider.timeout == 30
        assert provider.model == "deepseek-chat"

    def test_mock_factory_still_works(self):
        provider = get_provider("mock")
        assert isinstance(provider, MockAiProvider)


# ── MockAiProvider tests (existing) ───────────────────────────────────────────


class TestMockProvider:
    @pytest.mark.asyncio
    async def test_always_returns_wait(self):
        provider = MockAiProvider()
        result = await provider.decide(COMPACT_CONTEXT)
        assert result["action"] == "WAIT"
        assert result["confidence"] == 0.0

    @pytest.mark.asyncio
    async def test_ignores_input_always_safe(self):
        provider = MockAiProvider()
        result = await provider.decide(COMPACT_CONTEXT)
        assert result["action"] == "WAIT"
