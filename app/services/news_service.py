"""News service — provides news context for AI trading decisions.

Fetches recent headlines per symbol from Google News RSS (free, no API key
or registration) and caches them in ``news_cache`` for a short window so
every evaluation cycle doesn't re-hit the feed. Any fetch, parse, or DB
error falls back to an empty/UNKNOWN context for that symbol — news is a
decision INPUT, never something that should block or fail an evaluation.

We deliberately do NOT classify sentiment ourselves: the AI reads the raw
headline text and judges negativity per the system prompt's own rules
(regulatory warnings, investigations, profit warnings, etc.) — pre-labeling
sentiment here would just be a second, unverified guess layered on top.

``kapNews`` (KAP-specific regulatory disclosures) stays empty for now —
Google News search results aren't reliably tagged as KAP filings vs. general
press coverage. A future upgrade can populate it from Matriks' own
``AddNewsKeyword("KAP")`` feed (bot-side event, pushed to a new endpoint).
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import aiohttp
from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.db import NewsCache

logger = logging.getLogger(__name__)

_RSS_URL = "https://news.google.com/rss/search?q={query}&hl=tr&gl=TR&ceid=TR:tr"
_CACHE_TTL = timedelta(minutes=30)
_MAX_ITEMS_PER_SYMBOL = 5
_FETCH_TIMEOUT_SECONDS = 8


# ── Public interface ───────────────────────────────────────────────────────────


async def get_news_context(symbols: list[str]) -> dict[str, Any]:
    """Return news context for a list of symbols.

    Args:
        symbols: List of trading symbols (e.g. ``["THYAO", "AKBNK"]``).

    Returns:
        Dict keyed by symbol, each with ``latestNews`` (recent real
        headlines), ``kapNews`` (currently always empty — see module
        docstring), and ``sentiment`` (always ``"UNKNOWN"`` — the AI judges
        this itself from ``latestNews`` text).
    """
    news: dict[str, Any] = {}
    for symbol in symbols:
        normalized = symbol.strip().upper()
        try:
            items = await _get_or_refresh(normalized)
        except Exception:
            logger.exception("Failed to load news context for %s", normalized)
            items = []
        news[normalized] = {
            "latestNews": [_serialize_item(item) for item in items],
            "kapNews": [],
            "sentiment": "UNKNOWN",
        }
    return news


# ── Cache + fetch orchestration ─────────────────────────────────────────────────


async def _get_or_refresh(symbol: str) -> list[dict[str, Any]]:
    cached = await _load_fresh_cache(symbol)
    if cached is not None:
        return cached

    fetched = await _fetch_rss(symbol)
    await _store_cache(symbol, fetched)
    return fetched


async def _load_fresh_cache(symbol: str) -> list[dict[str, Any]] | None:
    """Return cached items if a fresh (< TTL) cache entry exists, else None."""
    try:
        async with async_session_factory() as session:
            cutoff = datetime.now(UTC) - _CACHE_TTL
            stmt = (
                select(NewsCache)
                .where(NewsCache.symbol == symbol, NewsCache.cached_at >= cutoff)
                .order_by(NewsCache.cached_at.desc())
                .limit(_MAX_ITEMS_PER_SYMBOL)
            )
            rows = (await session.execute(stmt)).scalars().all()
    except Exception:
        # news_cache may not exist yet in an environment without the table
        # migrated — degrade to live-fetch-only rather than failing.
        logger.exception("News cache read failed for %s", symbol)
        return None
    if not rows:
        return None
    return [
        {"title": row.title, "url": row.url, "source": row.source, "publishedAt": row.published_at}
        for row in rows
    ]


async def _store_cache(symbol: str, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    try:
        async with async_session_factory() as session:
            for item in items:
                session.add(
                    NewsCache(
                        symbol=symbol,
                        title=item["title"],
                        source=item.get("source"),
                        url=item.get("url"),
                        published_at=item.get("publishedAt"),
                    )
                )
            await session.commit()
    except Exception:
        logger.exception("News cache write failed for %s — continuing without cache", symbol)


def _serialize_item(item: dict[str, Any]) -> dict[str, Any]:
    published = item.get("publishedAt")
    return {
        "title": item["title"],
        "source": item.get("source"),
        "url": item.get("url"),
        "publishedAt": published.isoformat() if isinstance(published, datetime) else published,
    }


# ── RSS fetch + parse ────────────────────────────────────────────────────────────


async def _fetch_rss(symbol: str) -> list[dict[str, Any]]:
    url = _RSS_URL.format(query=quote(f"{symbol} hisse"))
    timeout = aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            body = await resp.text()
    return _parse_rss(body)[:_MAX_ITEMS_PER_SYMBOL]


def _parse_rss(xml_text: str) -> list[dict[str, Any]]:
    """Parse a Google News RSS feed into title/url/source/publishedAt items.

    Never raises on malformed individual items — a bad <item> is skipped,
    not fatal to the rest of the feed.
    """
    items: list[dict[str, Any]] = []
    root = ET.fromstring(xml_text)
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        link = (item.findtext("link") or "").strip() or None
        source_el = item.find("source")
        source = source_el.text.strip() if source_el is not None and source_el.text else None
        items.append(
            {
                "title": title,
                "url": link,
                "source": source,
                "publishedAt": _parse_pub_date(item.findtext("pubDate")),
            }
        )
    return items


def _parse_pub_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError):
        return None
