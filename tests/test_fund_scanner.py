"""Tests for the fund scanner service (mock) and its integration into signal payload."""

from __future__ import annotations

import json


from app.services.fund_scanner import get_fund_context


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: get_fund_context (mock)
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetFundContext:
    async def test_single_symbol_returns_mock_entry(self):
        result = await get_fund_context(["THYAO"])

        assert "THYAO" in result
        entry = result["THYAO"]
        assert entry["fundInterest"] == "UNKNOWN"
        assert entry["topFundsHolding"] == []
        assert entry["fundScore"] == 0

    async def test_multiple_symbols_each_get_separate_context(self):
        result = await get_fund_context(["THYAO", "AKBNK", "SASA"])

        assert result.keys() == {"THYAO", "AKBNK", "SASA"}
        for symbol in ("THYAO", "AKBNK", "SASA"):
            assert result[symbol]["fundInterest"] == "UNKNOWN"
            assert result[symbol]["fundScore"] == 0

    async def test_empty_list_returns_empty_dict(self):
        result = await get_fund_context([])

        assert result == {}

    async def test_context_is_serializable(self):
        result = await get_fund_context(["GARAN"])

        assert json.dumps(result)  # does not raise


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: _build_payload injects fund_context
# ═══════════════════════════════════════════════════════════════════════════════


class TestFundInPayload:
    def test_build_payload_with_fund_context(self):
        from app.models.signal import SignalRequest
        from app.services.evaluator import build_payload as _build_payload

        req = SignalRequest(
            requestId="test-1",
            symbol="THYAO",
            timeframe="1h",
            lastPrice=100.0,
            open=99.0,
            high=102.0,
            low=98.0,
            volume=1000.0,
            rsi=50.0,
            ema20=98.0,
            ema50=95.0,
        )

        funds = {
            "THYAO": {
                "fundInterest": "HIGH",
                "topFundsHolding": [
                    {
                        "fundCode": "TCD",
                        "fundName": "Tacirler Değişken Fon",
                        "weight": 8.5,
                    },
                ],
                "fundScore": 72,
            }
        }

        payload = _build_payload(req, fund_context=funds)

        assert "fundContext" in payload
        assert payload["fundContext"] == funds
        assert payload["fundContext"]["THYAO"]["fundInterest"] == "HIGH"
        assert payload["fundContext"]["THYAO"]["fundScore"] == 72

    def test_build_payload_without_fund_context_excluded(self):
        from app.models.signal import SignalRequest
        from app.services.evaluator import build_payload as _build_payload

        req = SignalRequest(
            requestId="test-1",
            symbol="THYAO",
            timeframe="1h",
            lastPrice=100.0,
            open=99.0,
            high=102.0,
            low=98.0,
            volume=1000.0,
            rsi=50.0,
            ema20=98.0,
            ema50=95.0,
        )

        payload = _build_payload(req)

        assert "fundContext" not in payload

    def test_build_payload_with_none_fund_context_excluded(self):
        from app.models.signal import SignalRequest
        from app.services.evaluator import build_payload as _build_payload

        req = SignalRequest(
            requestId="test-1",
            symbol="THYAO",
            timeframe="1h",
            lastPrice=100.0,
            open=99.0,
            high=102.0,
            low=98.0,
            volume=1000.0,
            rsi=50.0,
            ema20=98.0,
            ema50=95.0,
        )

        payload = _build_payload(req, fund_context=None)

        assert "fundContext" not in payload

    def test_both_contexts_injected_together(self):
        """When both news and fund context are supplied, both appear in payload."""
        from app.models.signal import SignalRequest
        from app.services.evaluator import build_payload as _build_payload

        req = SignalRequest(
            requestId="test-1",
            symbol="AKBNK",
            timeframe="1h",
            lastPrice=100.0,
            open=99.0,
            high=102.0,
            low=98.0,
            volume=1000.0,
            rsi=50.0,
            ema20=98.0,
            ema50=95.0,
        )

        news = {"AKBNK": {"latestNews": [], "kapNews": [], "sentiment": "NEUTRAL"}}
        funds = {
            "AKBNK": {"fundInterest": "MEDIUM", "topFundsHolding": [], "fundScore": 45}
        }

        payload = _build_payload(req, news_context=news, fund_context=funds)

        assert "newsContext" in payload
        assert "fundContext" in payload
        assert payload["newsContext"]["AKBNK"]["sentiment"] == "NEUTRAL"
        assert payload["fundContext"]["AKBNK"]["fundInterest"] == "MEDIUM"
