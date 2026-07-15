"""Tests for the news service (Google News RSS integration) and its
integration into signal endpoint payloads.

The real network call (_fetch_rss) is always mocked here — these tests must
never depend on external network availability. See _fetch_rss's own
live-fetch behavior verified manually against the real feed during
development; only pure parsing (_parse_rss) and the mocked orchestration
logic are covered by the automated suite.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import NewsCache
from app.services.news_service import _parse_rss, get_news_context


@pytest.fixture(autouse=True)
def _reset_db():
    asyncio.run(drop_all())
    asyncio.run(init_db())
    yield
    asyncio.run(drop_all())
    asyncio.run(init_db())


@pytest.fixture(autouse=True)
def _disable_fulltext(monkeypatch):
    """Keep the suite hermetic: never let full-text enrichment hit the network.

    _fetch_rss is mocked per-test, but _get_or_refresh also calls
    _enrich_with_fulltext, which would otherwise make real HTTP requests to the
    mock item URLs. Replace it with an identity passthrough.
    """

    async def _identity(items):
        return items

    monkeypatch.setattr("app.services.news_service._enrich_with_fulltext", _identity)


async def _mock_fetch(symbol: str) -> list[dict]:
    return [
        {
            "title": f"{symbol} test headline",
            "url": "https://example.com/1",
            "source": "Test Source",
            "publishedAt": datetime.now(UTC),
        }
    ]


async def _failing_fetch(symbol: str) -> list[dict]:
    raise RuntimeError("network error")


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: _parse_rss (pure function, no network)
# ═══════════════════════════════════════════════════════════════════════════════

_SAMPLE_RSS = """<?xml version="1.0"?>
<rss version="2.0">
<channel>
  <item>
    <title>THYAO hisse yorumu - Kaynak</title>
    <link>https://news.google.com/rss/articles/abc</link>
    <pubDate>Wed, 09 Jul 2026 08:00:00 GMT</pubDate>
    <source url="https://example.com">Ornek Kaynak</source>
  </item>
  <item>
    <title></title>
    <link>https://news.google.com/rss/articles/empty-title</link>
  </item>
</channel>
</rss>
"""


class TestParseRss:
    def test_parses_title_link_source_date(self):
        items = _parse_rss(_SAMPLE_RSS)

        assert len(items) == 1  # the empty-title item is skipped
        assert items[0]["title"] == "THYAO hisse yorumu - Kaynak"
        assert items[0]["url"] == "https://news.google.com/rss/articles/abc"
        assert items[0]["source"] == "Ornek Kaynak"
        assert items[0]["publishedAt"] is not None
        assert items[0]["publishedAt"].year == 2026

    def test_no_items_returns_empty_list(self):
        assert _parse_rss("<rss><channel></channel></rss>") == []

    def test_malformed_xml_raises(self):
        with pytest.raises(Exception):
            _parse_rss("not xml at all <<<")


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: get_news_context (network mocked)
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetNewsContext:
    @pytest.mark.asyncio
    async def test_single_symbol_returns_fetched_items(self, monkeypatch):
        monkeypatch.setattr("app.services.news_service._fetch_rss", _mock_fetch)

        result = await get_news_context(["THYAO"])

        assert "THYAO" in result
        assert len(result["THYAO"]["latestNews"]) == 1
        assert result["THYAO"]["latestNews"][0]["title"] == "THYAO test headline"
        assert result["THYAO"]["kapNews"] == []
        assert result["THYAO"]["sentiment"] == "UNKNOWN"

    @pytest.mark.asyncio
    async def test_multiple_symbols_each_get_separate_context(self, monkeypatch):
        monkeypatch.setattr("app.services.news_service._fetch_rss", _mock_fetch)

        result = await get_news_context(["THYAO", "AKBNK", "SISE"])

        assert len(result) == 3
        for symbol in ("THYAO", "AKBNK", "SISE"):
            assert symbol in result
            assert result[symbol]["latestNews"][0]["title"] == f"{symbol} test headline"

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr("app.services.news_service._fetch_rss", _mock_fetch)
        result = await get_news_context([])
        assert result == {}

    @pytest.mark.asyncio
    async def test_context_is_serializable(self, monkeypatch):
        monkeypatch.setattr("app.services.news_service._fetch_rss", _mock_fetch)

        result = await get_news_context(["THYAO"])
        dumped = json.dumps(result)

        assert '"THYAO"' in dumped
        assert '"UNKNOWN"' in dumped

    @pytest.mark.asyncio
    async def test_fetch_failure_degrades_to_empty(self, monkeypatch):
        monkeypatch.setattr("app.services.news_service._fetch_rss", _failing_fetch)

        result = await get_news_context(["THYAO"])

        assert result["THYAO"] == {
            "latestNews": [],
            "kapNews": [],
            "sentiment": "UNKNOWN",
            "trustBoundary": "UNTRUSTED_EXTERNAL_CONTENT",
        }

    @pytest.mark.asyncio
    async def test_symbol_normalized_to_uppercase(self, monkeypatch):
        monkeypatch.setattr("app.services.news_service._fetch_rss", _mock_fetch)

        result = await get_news_context(["thyao"])

        assert "THYAO" in result
        assert "thyao" not in result


# ═══════════════════════════════════════════════════════════════════════════════
# Unit: caching behavior
# ═══════════════════════════════════════════════════════════════════════════════


class TestNewsCaching:
    @pytest.mark.asyncio
    async def test_second_call_within_ttl_uses_cache_not_network(self, monkeypatch):
        call_count = 0

        async def _counting_fetch(symbol: str) -> list[dict]:
            nonlocal call_count
            call_count += 1
            return [
                {
                    "title": "Cacheable headline",
                    "url": "https://example.com/1",
                    "source": "Test",
                    "publishedAt": datetime.now(UTC),
                }
            ]

        monkeypatch.setattr("app.services.news_service._fetch_rss", _counting_fetch)

        await get_news_context(["THYAO"])
        await get_news_context(["THYAO"])

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_stale_cache_triggers_refetch(self, monkeypatch):
        async with async_session_factory() as session:
            row = NewsCache(
                symbol="THYAO",
                title="Old headline",
                source="Old",
                url="https://example.com/old",
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            await session.execute(
                update(NewsCache)
                .where(NewsCache.id == row.id)
                .values(cached_at=datetime.now(UTC) - timedelta(hours=2))
            )
            await session.commit()

        call_count = 0

        async def _counting_fetch(symbol: str) -> list[dict]:
            nonlocal call_count
            call_count += 1
            return [
                {
                    "title": "Fresh headline",
                    "url": "https://example.com/new",
                    "source": "New",
                    "publishedAt": datetime.now(UTC),
                }
            ]

        monkeypatch.setattr("app.services.news_service._fetch_rss", _counting_fetch)

        result = await get_news_context(["THYAO"])

        assert call_count == 1
        assert result["THYAO"]["latestNews"][0]["title"] == "Fresh headline"

    @pytest.mark.asyncio
    async def test_db_cache_failure_falls_back_to_live_fetch(self, monkeypatch):
        """If the cache table read/write fails (e.g. table doesn't exist
        yet in a fresh environment), still return live-fetched results
        instead of raising — the cache is optional infrastructure."""

        def _boom():
            raise RuntimeError("db unavailable")

        monkeypatch.setattr("app.services.news_service.async_session_factory", _boom)
        monkeypatch.setattr("app.services.news_service._fetch_rss", _mock_fetch)

        result = await get_news_context(["THYAO"])

        assert result["THYAO"]["latestNews"][0]["title"] == "THYAO test headline"


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: signal endpoint payload includes news_context
# ═══════════════════════════════════════════════════════════════════════════════


class TestNewsInPayload:
    """Verify _build_payload injects news_context when present."""

    def test_build_payload_with_news_context(self):
        from app.models.signal import SignalMode, SignalRequest
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
            mode=SignalMode.MANUAL,
        )

        news = {"THYAO": {"latestNews": [], "kapNews": [], "sentiment": "UNKNOWN"}}

        payload = _build_payload(req, news_context=news)

        assert "newsContext" in payload
        assert payload["newsContext"] == news
        assert payload["newsContext"]["THYAO"]["sentiment"] == "UNKNOWN"

    def test_build_payload_without_news_context_still_works(self):
        from app.models.signal import SignalMode, SignalRequest
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
            mode=SignalMode.MANUAL,
        )

        payload = _build_payload(req)

        assert "newsContext" not in payload
        assert payload["symbol"] == "THYAO"

    def test_build_payload_with_none_news_context_excluded(self):
        from app.models.signal import SignalMode, SignalRequest
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
            mode=SignalMode.MANUAL,
        )

        payload = _build_payload(req, news_context=None)

        assert "newsContext" not in payload

    def test_build_payload_includes_technical_features_when_present(self):
        from app.models.signal import SignalMode, SignalRequest
        from app.services.evaluator import build_payload as _build_payload

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

        technical = payload["technicalFeatures"]
        assert "alphaTrendSignal" not in payload
        assert "depthQueueDropPct" not in payload
        assert technical["alphaTrendSignal"] == "BUY"
        assert technical["indicatorConsensus"] == "BUY"
        assert technical["natr"] == 2.4
        assert technical["depthQueueDropPct"] == 12.0
        assert technical["schemaVersion"] == "technical-features-v2"
        assert technical["indicatorBuyCount"] == 4
