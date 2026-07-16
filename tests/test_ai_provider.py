"""Unit tests for DeepSeek provider — async API calls, parsing, fallback."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
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
            result = await provider.decide(
                COMPACT_CONTEXT | {"technical": {"rsi": 22.0}}
            )

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
            result = await provider.decide(
                COMPACT_CONTEXT | {"symbol": "AKBNK", "technical": {"rsi": 78.0}}
            )

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
    async def test_network_error_retries_then_succeeds(self, monkeypatch):
        """First attempt fails, second (within max_attempts) succeeds."""
        monkeypatch.setattr(
            __import__("asyncio"), "sleep", AsyncMock(return_value=None)
        )
        provider = _provider(max_attempts=2)
        session = _mock_session()
        success_ctx = AsyncMock()
        success_ctx.__aenter__ = AsyncMock(
            return_value=_fake_resp(
                status=200,
                json_body={
                    "choices": [
                        {
                            "message": {
                                "content": '{"action": "BUY", "confidence": 80, "reason": "recovered"}'
                            }
                        }
                    ]
                },
            )
        )
        success_ctx.__aexit__ = AsyncMock(return_value=None)
        session.post = MagicMock(
            side_effect=[
                __import__("aiohttp").ClientError("timeout"),
                success_ctx,
            ]
        )

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = session
            result = await provider.decide(COMPACT_CONTEXT)

        assert result["action"] == "BUY"
        assert session.post.call_count == 2
        assert provider.consecutive_failures == 0
        assert provider.is_degraded is False

    @pytest.mark.asyncio
    async def test_exhausts_max_attempts_then_falls_back(self, monkeypatch):
        sleep_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(__import__("asyncio"), "sleep", sleep_mock)
        provider = _provider(max_attempts=2)
        session = _mock_session()
        session.post.side_effect = __import__("aiohttp").ClientError("still down")

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = session
            result = await provider.decide(COMPACT_CONTEXT)

        assert result["action"] == "WAIT"
        assert session.post.call_count == 2
        assert sleep_mock.call_count == 1
        assert provider.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_backoff_is_exponential_between_attempts(self, monkeypatch):
        sleep_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(__import__("asyncio"), "sleep", sleep_mock)
        provider = _provider(max_attempts=3)
        session = _mock_session()
        session.post.side_effect = __import__("aiohttp").ClientError("down")

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = session
            await provider.decide(COMPACT_CONTEXT)

        assert [call.args[0] for call in sleep_mock.call_args_list] == [1, 2]

    @pytest.mark.asyncio
    async def test_four_xx_error_does_not_retry_but_counts_as_failure(self):
        provider = _provider(max_attempts=2)
        resp = _fake_resp(status=401, content='{"error": "unauthorized"}')

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = _mock_session(resp)
            result = await provider.decide(COMPACT_CONTEXT)

        assert result["action"] == "WAIT"
        assert provider.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_becomes_degraded_after_threshold_consecutive_failures(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            __import__("asyncio"), "sleep", AsyncMock(return_value=None)
        )
        provider = _provider(max_attempts=1, degraded_threshold=3)
        session = _mock_session()
        session.post.side_effect = __import__("aiohttp").ClientError("down")

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = session
            for _ in range(2):
                await provider.decide(COMPACT_CONTEXT)
            assert provider.is_degraded is False
            await provider.decide(COMPACT_CONTEXT)

        assert provider.consecutive_failures == 3
        assert provider.is_degraded is True

    @pytest.mark.asyncio
    async def test_degraded_provider_skips_network_call(self, monkeypatch):
        monkeypatch.setattr(
            __import__("asyncio"), "sleep", AsyncMock(return_value=None)
        )
        provider = _provider(
            max_attempts=1, degraded_threshold=1, probe_interval_seconds=3600
        )
        session = _mock_session()
        session.post.side_effect = __import__("aiohttp").ClientError("down")

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = session
            await provider.decide(COMPACT_CONTEXT)  # first failure -> degraded
            assert provider.is_degraded is True
            call_count_before = session.post.call_count
            result = await provider.decide(COMPACT_CONTEXT)

        assert session.post.call_count == call_count_before  # no new network call
        assert result["action"] == "WAIT"
        assert "degraded" in result["reason"].lower()

    @pytest.mark.asyncio
    async def test_degraded_provider_probes_after_interval_and_recovers(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            __import__("asyncio"), "sleep", AsyncMock(return_value=None)
        )
        provider = _provider(
            max_attempts=1, degraded_threshold=1, probe_interval_seconds=0
        )
        session = _mock_session()
        session.post.side_effect = __import__("aiohttp").ClientError("down")

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = session
            await provider.decide(COMPACT_CONTEXT)  # first failure -> degraded
        assert provider.is_degraded is True

        success_ctx = AsyncMock()
        success_ctx.__aenter__ = AsyncMock(
            return_value=_fake_resp(
                status=200,
                json_body={
                    "choices": [
                        {
                            "message": {
                                "content": '{"action": "WAIT", "confidence": 50, "reason": "probe ok"}'
                            }
                        }
                    ]
                },
            )
        )
        success_ctx.__aexit__ = AsyncMock(return_value=None)
        session.post = MagicMock(return_value=success_ctx)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = session
            result = await provider.decide(COMPACT_CONTEXT)

        assert session.post.call_count == 1  # probe attempted, not skipped
        assert result["reason"] == "probe ok"
        assert provider.is_degraded is False
        assert provider.consecutive_failures == 0

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
                    "period": {
                        "requested": "MIN5",
                        "actual": "MIN5",
                        "mismatch": False,
                    },
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

    def test_never_reports_degraded(self):
        provider = MockAiProvider()
        assert provider.is_degraded is False
        assert provider.consecutive_failures == 0


class TestAiProviderStatus:
    def test_get_ai_provider_status_shape(self, monkeypatch):
        import app.services.ai_provider as ai_provider_module

        monkeypatch.setattr(ai_provider_module, "_default_provider", MockAiProvider())
        status = ai_provider_module.get_ai_provider_status()

        assert status == {
            "providerName": "MockAiProvider",
            "isDegraded": False,
            "consecutiveFailures": 0,
            "toolCallingEnabled": False,
        }

    def test_get_ai_provider_status_reflects_degraded_deepseek(self, monkeypatch):
        import app.services.ai_provider as ai_provider_module

        degraded = _provider(max_attempts=1, degraded_threshold=1)
        degraded.consecutive_failures = 1
        monkeypatch.setattr(ai_provider_module, "_default_provider", degraded)

        status = ai_provider_module.get_ai_provider_status()

        assert status["providerName"] == "DeepSeekProvider"
        assert status["isDegraded"] is True
        assert status["consecutiveFailures"] == 1


# ── Tool-calling döngüsü (v2 Faz 2) ──────────────────────────────────────────


def _tool_call(name: str, args: dict, call_id: str = "tc-1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _msg_resp(message: dict):
    return _fake_resp(status=200, json_body={"choices": [{"message": message}]})


FINAL_BUY = {
    "content": '{"action": "BUY", "confidence": 85, "reason": "tool-verified"}'
}


class TestDeepSeekToolLoop:
    """Bütçeli tool-calling döngüsü: 4 tur / 6 çağrı / 12 sn wall-clock."""

    def _tools_provider(self, **kwargs) -> DeepSeekProvider:
        defaults = {"tools_enabled": True, "tool_budget_seconds": 12.0}
        defaults.update(kwargs)
        return _provider(**defaults)

    def _patch_call_tool(self, monkeypatch):
        calls: list[dict] = []

        async def fake_call_tool(name, args, *, caller, request_id=None, symbol_scope=None):
            calls.append(
                {
                    "name": name,
                    "args": args,
                    "caller": caller,
                    "request_id": request_id,
                    "symbol_scope": symbol_scope,
                }
            )
            return {"tool": name, "result": {"ok": True, "value": 42}}

        monkeypatch.setattr("app.tools.call_tool", fake_call_tool)
        return calls

    @pytest.mark.asyncio
    async def test_tools_disabled_sends_legacy_body_without_tools_key(self):
        provider = _provider()  # settings default: tools kapalı
        assert provider.tools_enabled is False
        session = _mock_session()
        with patch("aiohttp.ClientSession", return_value=session):
            result = await provider.decide(COMPACT_CONTEXT)
        assert result["action"] == "BUY"
        body = session.post.call_args.kwargs["json"]
        assert "tools" not in body
        assert "tool_choice" not in body

    @pytest.mark.asyncio
    async def test_two_tool_calls_then_final_decision(self, monkeypatch):
        calls = self._patch_call_tool(monkeypatch)
        provider = self._tools_provider()
        round1 = _mock_session(
            _msg_resp(
                {
                    "content": None,
                    "tool_calls": [
                        _tool_call("get_indicators", {"symbol": "THYAO"}, "tc-1"),
                        _tool_call("get_depth", {"symbol": "THYAO"}, "tc-2"),
                    ],
                }
            )
        )
        round2 = _mock_session(_msg_resp(FINAL_BUY))
        with patch("aiohttp.ClientSession", side_effect=[round1, round2]):
            result = await provider.decide(COMPACT_CONTEXT)

        assert result["action"] == "BUY"
        assert [c["name"] for c in calls] == ["get_indicators", "get_depth"]
        assert all(c["caller"] == "deepseek" for c in calls)
        assert all(c["symbol_scope"] == "THYAO" for c in calls)
        assert result["_audit_raw_response"]["toolCallsUsed"] == [
            "get_indicators",
            "get_depth",
        ]
        # İkinci turun gövdesi tool sonuçlarını içermeli.
        body2 = round2.post.call_args.kwargs["json"]
        roles = [m["role"] for m in body2["messages"]]
        assert roles == ["system", "user", "assistant", "tool", "tool"]
        assert body2["tool_choice"] == "auto"

    @pytest.mark.asyncio
    async def test_endless_tool_calls_forced_final_after_max_rounds(self, monkeypatch):
        calls = self._patch_call_tool(monkeypatch)
        provider = self._tools_provider()
        tool_rounds = [
            _mock_session(
                _msg_resp(
                    {
                        "content": None,
                        "tool_calls": [
                            _tool_call("get_snapshot", {"symbol": "THYAO"}, f"tc-{i}")
                        ],
                    }
                )
            )
            for i in range(4)
        ]
        final_round = _mock_session(_msg_resp(FINAL_BUY))
        with patch(
            "aiohttp.ClientSession", side_effect=[*tool_rounds, final_round]
        ):
            result = await provider.decide(COMPACT_CONTEXT)

        assert result["action"] == "BUY"
        assert len(calls) == 4  # tur başına 1 çağrı, 4 turda kesildi
        final_body = final_round.post.call_args.kwargs["json"]
        assert final_body["tool_choice"] == "none"
        # Bütçe nudge'ı son tura eklenmiş olmalı.
        assert final_body["messages"][-1]["content"].startswith(
            "Tool budget exhausted"
        )

    @pytest.mark.asyncio
    async def test_tool_error_is_fed_back_not_fatal(self, monkeypatch):
        async def failing_call_tool(name, args, *, caller, request_id=None, symbol_scope=None):
            return {"tool": name, "error": "tool failed: gateway down"}

        monkeypatch.setattr("app.tools.call_tool", failing_call_tool)
        provider = self._tools_provider()
        round1 = _mock_session(
            _msg_resp(
                {
                    "content": None,
                    "tool_calls": [_tool_call("get_news", {"symbol": "THYAO"})],
                }
            )
        )
        round2 = _mock_session(_msg_resp(FINAL_BUY))
        with patch("aiohttp.ClientSession", side_effect=[round1, round2]):
            result = await provider.decide(COMPACT_CONTEXT)

        assert result["action"] == "BUY"
        body2 = round2.post.call_args.kwargs["json"]
        tool_msg = body2["messages"][-1]
        assert tool_msg["role"] == "tool"
        assert "gateway down" in tool_msg["content"]

    @pytest.mark.asyncio
    async def test_zero_budget_forces_immediate_final(self, monkeypatch):
        self._patch_call_tool(monkeypatch)
        provider = self._tools_provider(tool_budget_seconds=0.0)
        only_round = _mock_session(_msg_resp(FINAL_BUY))
        with patch("aiohttp.ClientSession", return_value=only_round):
            result = await provider.decide(COMPACT_CONTEXT)

        assert result["action"] == "BUY"
        body = only_round.post.call_args.kwargs["json"]
        assert body["tool_choice"] == "none"

    @pytest.mark.asyncio
    async def test_network_error_returns_wait_and_counts_degraded(self, monkeypatch):
        self._patch_call_tool(monkeypatch)
        provider = self._tools_provider(degraded_threshold=1)
        session = _mock_session()
        session.post = MagicMock(side_effect=aiohttp.ClientError("conn reset"))
        with patch("aiohttp.ClientSession", return_value=session):
            result = await provider.decide(COMPACT_CONTEXT)

        assert result["action"] == "WAIT"
        assert "Network error" in result["reason"]
        assert provider.is_degraded is True

    @pytest.mark.asyncio
    async def test_unparseable_final_returns_wait(self, monkeypatch):
        self._patch_call_tool(monkeypatch)
        provider = self._tools_provider()
        session = _mock_session(_msg_resp({"content": "not json at all"}))
        with patch("aiohttp.ClientSession", return_value=session):
            result = await provider.decide(COMPACT_CONTEXT)

        assert result["action"] == "WAIT"
        assert "Could not parse model response" in result["reason"]

    @pytest.mark.asyncio
    async def test_evaluation_request_id_forwarded_to_call_tool(self, monkeypatch):
        """Fix #5: değerlendirme request_id'si tool audit'ine geçirilir."""
        calls = self._patch_call_tool(monkeypatch)
        provider = self._tools_provider()
        round1 = _mock_session(
            _msg_resp(
                {
                    "content": None,
                    "tool_calls": [_tool_call("get_snapshot", {"symbol": "THYAO"})],
                }
            )
        )
        round2 = _mock_session(_msg_resp(FINAL_BUY))
        with patch("aiohttp.ClientSession", side_effect=[round1, round2]):
            await provider.decide(COMPACT_CONTEXT, request_id="eval-req-77")
        assert calls[0]["request_id"] == "eval-req-77"

    @pytest.mark.asyncio
    async def test_hard_timeout_returns_wait_when_round_hangs(self, monkeypatch):
        """Fix #5: takılan bir LLM turu 12 sn'lik kesin asyncio.timeout ile
        kesilir ve WAIT fallback döner."""
        self._patch_call_tool(monkeypatch)
        provider = self._tools_provider(tool_budget_seconds=0.2)

        async def _hang(*_a, **_k):
            await asyncio.sleep(5)
            return None, "unreached"

        monkeypatch.setattr(provider, "_tool_round_completion", _hang)
        result = await provider.decide(COMPACT_CONTEXT)
        assert result["action"] == "WAIT"
        assert "hard timeout" in result["reason"]

    @pytest.mark.asyncio
    async def test_tool_loop_uses_tool_aware_system_prompt(self, monkeypatch):
        self._patch_call_tool(monkeypatch)
        provider = self._tools_provider()
        session = _mock_session(_msg_resp(FINAL_BUY))
        with patch("aiohttp.ClientSession", return_value=session):
            await provider.decide(COMPACT_CONTEXT)

        body = session.post.call_args.kwargs["json"]
        assert "TOOLS" in body["messages"][0]["content"]
        assert isinstance(body["tools"], list)
        names = {t["function"]["name"] for t in body["tools"]}
        assert "get_snapshot" in names
        assert "get_account_summary" not in names
