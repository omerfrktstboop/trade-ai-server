"""Tests for the news service (mock) and its integration into signal endpoint."""

from __future__ import annotations

import pytest

from app.services.news_service import get_news_context


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: get_news_context
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetNewsContext:
    """Mock news service always returns empty lists and UNKNOWN sentiment."""

    @pytest.mark.asyncio
    async def test_single_symbol_returns_empty_context(self):
        result = await get_news_context(["THYAO"])

        assert "THYAO" in result
        assert result["THYAO"]["latestNews"] == []
        assert result["THYAO"]["kapNews"] == []
        assert result["THYAO"]["sentiment"] == "UNKNOWN"

    @pytest.mark.asyncio
    async def test_multiple_symbols_each_get_separate_context(self):
        result = await get_news_context(["THYAO", "AKBNK", "SISE"])

        assert len(result) == 3
        for symbol in ("THYAO", "AKBNK", "SISE"):
            assert symbol in result
            assert result[symbol] == {
                "latestNews": [],
                "kapNews": [],
                "sentiment": "UNKNOWN",
            }

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty_dict(self):
        result = await get_news_context([])
        assert result == {}

    @pytest.mark.asyncio
    async def test_context_is_serializable(self):
        import json

        result = await get_news_context(["THYAO"])
        dumped = json.dumps(result)
        assert '"THYAO"' in dumped
        assert '"UNKNOWN"' in dumped


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: signal endpoint payload includes news_context
# ═══════════════════════════════════════════════════════════════════════════════


class TestNewsInPayload:
    """Verify _build_payload injects news_context when present."""

    def test_build_payload_with_news_context(self):
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

        news = {"THYAO": {"latestNews": [], "kapNews": [], "sentiment": "UNKNOWN"}}

        payload = _build_payload(req, news_context=news)

        assert "newsContext" in payload
        assert payload["newsContext"] == news
        assert payload["newsContext"]["THYAO"]["sentiment"] == "UNKNOWN"

    def test_build_payload_without_news_context_still_works(self):
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

        assert "newsContext" not in payload
        assert payload["symbol"] == "THYAO"

    def test_build_payload_with_none_news_context_excluded(self):
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

        payload = _build_payload(req, news_context=None)

        assert "newsContext" not in payload

    def test_build_payload_includes_technical_features_when_present(self):
        from app.models.signal import SignalMode, SignalRequest
        from app.routers.signal import _build_payload

        req = SignalRequest(
            requestId="test-technical-1",
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
            alphaTrendSignal="BUY",
            indicatorBuyCount=4,
            indicatorSellCount=1,
            indicatorConsensus="BUY",
            natr=2.4,
            depthQueueDropPct=12.0,
            mode=SignalMode.MANUAL,
        )

        payload = _build_payload(req)

        assert payload["alphaTrendSignal"] == "BUY"
        assert payload["indicatorConsensus"] == "BUY"
        assert payload["natr"] == 2.4
        assert payload["depthQueueDropPct"] == 12.0
        assert payload["technicalFeatures"]["schemaVersion"] == "technical-features-v1"
        assert payload["technicalFeatures"]["indicatorBuyCount"] == 4
