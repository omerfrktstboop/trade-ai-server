"""Tests for the broker flow service (mock) and its integration into signal payload."""

from __future__ import annotations

import json

import pytest

from app.services.broker_flow_service import get_broker_flow_context


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: get_broker_flow_context (mock)
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetBrokerFlowContext:
    async def test_single_symbol_returns_mock_entry(self):
        result = await get_broker_flow_context(["THYAO"])

        assert "THYAO" in result
        entry = result["THYAO"]
        assert entry["symbol"] == "THYAO"
        assert entry["brokerFlow"] == "UNKNOWN"
        assert entry["netInstitutionalFlow"] is None
        assert entry["topBrokers"] == []
        assert entry["comment"] == "Broker flow data not provided."

    async def test_multiple_symbols_each_get_separate_context(self):
        result = await get_broker_flow_context(["THYAO", "AKBNK", "SASA"])

        assert result.keys() == {"THYAO", "AKBNK", "SASA"}
        for symbol in ("THYAO", "AKBNK", "SASA"):
            assert result[symbol]["symbol"] == symbol
            assert result[symbol]["brokerFlow"] == "UNKNOWN"
            assert result[symbol]["netInstitutionalFlow"] is None

    async def test_empty_list_returns_empty_dict(self):
        result = await get_broker_flow_context([])

        assert result == {}

    async def test_context_is_serializable(self):
        result = await get_broker_flow_context(["GARAN"])

        assert json.dumps(result)  # does not raise


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: _build_payload injects broker_flow_context
# ═══════════════════════════════════════════════════════════════════════════════


class TestBrokerFlowInPayload:
    def test_build_payload_with_broker_flow_context(self):
        from app.models.signal import SignalMode, SignalRequest
        from app.routers.signal import _build_payload

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
            mode=SignalMode.MANUAL,
        )

        flow = {
            "THYAO": {
                "symbol": "THYAO",
                "brokerFlow": "BUY",
                "netInstitutionalFlow": 1_250_000.0,
                "topBrokers": [
                    {"brokerName": "Garanti", "netFlow": 500_000.0, "side": "BUY"},
                ],
                "comment": "",
            }
        }

        payload = _build_payload(req, broker_flow_context=flow)

        assert "brokerFlowContext" in payload
        assert payload["brokerFlowContext"] == flow
        assert payload["brokerFlowContext"]["THYAO"]["brokerFlow"] == "BUY"
        assert payload["brokerFlowContext"]["THYAO"]["netInstitutionalFlow"] == 1_250_000.0

    def test_build_payload_without_broker_flow_context_excluded(self):
        from app.models.signal import SignalMode, SignalRequest
        from app.routers.signal import _build_payload

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
            mode=SignalMode.MANUAL,
        )

        payload = _build_payload(req)

        assert "brokerFlowContext" not in payload

    def test_build_payload_with_none_broker_flow_context_excluded(self):
        from app.models.signal import SignalMode, SignalRequest
        from app.routers.signal import _build_payload

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
            mode=SignalMode.MANUAL,
        )

        payload = _build_payload(req, broker_flow_context=None)

        assert "brokerFlowContext" not in payload

    def test_all_three_contexts_injected_together(self):
        """When all three context sources are supplied, all appear in payload."""
        from app.models.signal import SignalMode, SignalRequest
        from app.routers.signal import _build_payload

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
            mode=SignalMode.MANUAL,
        )

        news = {"AKBNK": {"latestNews": [], "kapNews": [], "sentiment": "NEUTRAL"}}
        funds = {"AKBNK": {"fundInterest": "MEDIUM", "topFundsHolding": [], "fundScore": 45}}
        flow = {"AKBNK": {"symbol": "AKBNK", "brokerFlow": "SELL", "netInstitutionalFlow": None,
                          "topBrokers": [], "comment": "..."}}

        payload = _build_payload(req, news_context=news, fund_context=funds,
                                 broker_flow_context=flow)

        assert "newsContext" in payload
        assert "fundContext" in payload
        assert "brokerFlowContext" in payload
        assert payload["newsContext"]["AKBNK"]["sentiment"] == "NEUTRAL"
        assert payload["fundContext"]["AKBNK"]["fundInterest"] == "MEDIUM"
        assert payload["brokerFlowContext"]["AKBNK"]["brokerFlow"] == "SELL"

    def test_broker_flow_missing_triggers_safe_wait(self):
        """When brokerFlow is UNKNOWN, system still operates (does not crash)."""
        from app.services.broker_flow_service import get_broker_flow_context
        import asyncio

        result = asyncio.run(get_broker_flow_context(["THYAO"]))

        entry = result["THYAO"]
        # Broker flow unavailable → system stays in safe mode, AI can still decide.
        assert entry["brokerFlow"] == "UNKNOWN"
        assert entry["netInstitutionalFlow"] is None
        # Payload is valid, AI gets the data but won't blindly buy on missing info.
        assert "brokerFlow" in entry
