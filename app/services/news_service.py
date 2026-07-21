"""News service — provides news context for AI trading decisions.

Fetches recent BIST-focused headlines per symbol from Google News RSS (free,
no API key or registration), rejects headlines that do not name the requested
symbol, cleans the HTML summary out of each item, and — on a cache
miss only — makes a bounded best-effort attempt to pull the article body so
the AI reads more than just a headline. Results are cached in ``news_cache``
for a short window so every evaluation cycle doesn't re-hit the feed. Any
fetch, parse, or DB error falls back to an empty/UNKNOWN context for that
symbol — news is a decision INPUT, never something that should block or fail
an evaluation.

We deliberately do NOT classify sentiment ourselves: the AI reads the raw
headline + summary text and judges negativity per the system prompt's own
rules (regulatory warnings, investigations, profit warnings, etc.) —
pre-labeling sentiment here would just be a second, unverified guess.

``kapNews`` (KAP-specific regulatory disclosures) stays empty for now —
Google News search results aren't reliably tagged as KAP filings vs. general
press coverage. A future upgrade can populate it from Matriks' own
``AddNewsKeyword("KAP")`` feed (bot-side event, pushed to a new endpoint).
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import re
import socket
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlparse

import aiohttp
from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.db import NewsCache
from app.services.decision_gate import decision_cache

logger = logging.getLogger(__name__)

_RSS_URL = "https://news.google.com/rss/search?q={query}&hl=tr&gl=TR&ceid=TR:tr"
_CACHE_TTL = timedelta(minutes=30)
_MAX_ITEMS_PER_SYMBOL = 5
# En önemli N haber, AI payload'una son 24 saatlik pencereden verilir.
_PAYLOAD_WINDOW = timedelta(hours=24)
_PAYLOAD_TOP_N = 3
_FETCH_TIMEOUT_SECONDS = 8

# Tam metin çekimi: sadece cache-miss'te ve en fazla ilk N haber için denenir,
# yani TTL başına sembol başına birkaç istek. Başarısızlık → özet metne düşer.
_FULLTEXT_ENABLED = True
_FULLTEXT_TIMEOUT_SECONDS = 6
_FULLTEXT_MAX_CHARS = 1500
_FULLTEXT_TOP_N = 3

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# <script>/<style> blokları içeriğiyle birlikte silinmeli.
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)


# ── Public interface ───────────────────────────────────────────────────────────


async def get_news_context(symbols: list[str]) -> dict[str, Any]:
    """Return news context for a list of symbols.

    Args:
        symbols: List of trading symbols (e.g. ``["THYAO", "AKBNK"]``).

    Returns:
        Dict keyed by symbol, each with ``latestNews`` (the most important
        recent items — title/summary/content/source/url within the last 24h,
        capped at 3), ``kapNews`` (currently always empty — see module
        docstring), and ``sentiment`` (always ``"UNKNOWN"`` — the AI judges
        this itself from ``latestNews`` text).
    """
    news: dict[str, Any] = {}
    for symbol in symbols:
        normalized = symbol.strip().upper()
        try:
            items = _filter_relevant_items(
                normalized, await _get_or_refresh(normalized)
            )
        except Exception:
            logger.exception("Failed to load news context for %s", normalized)
            items = []
        top = _select_top_recent(items)
        news[normalized] = {
            "latestNews": [_serialize_item(item) for item in top],
            "kapNews": [],
            "sentiment": "UNKNOWN",
            "trustBoundary": "UNTRUSTED_EXTERNAL_CONTENT",
        }
    return news


# ── Cache + fetch orchestration ─────────────────────────────────────────────────


async def _get_or_refresh(symbol: str) -> list[dict[str, Any]]:
    cached = await _load_fresh_cache(symbol)
    if cached is not None:
        return _filter_relevant_items(symbol, cached)

    fetched = _filter_relevant_items(symbol, await _fetch_rss(symbol))
    # Tam metin zenginleştirme yalnızca taze çekimde (cache-miss) yapılır.
    fetched = await _enrich_with_fulltext(fetched)
    await _store_cache(symbol, fetched)
    decision_cache.clear(symbol)
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
        {
            "title": row.title,
            "content": row.content,
            "url": row.url,
            "source": row.source,
            "publishedAt": row.published_at,
        }
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
                        content=item.get("content"),
                        source=item.get("source"),
                        url=item.get("url"),
                        published_at=item.get("publishedAt"),
                    )
                )
            await session.commit()
    except Exception:
        logger.exception(
            "News cache write failed for %s — continuing without cache", symbol
        )


def _select_top_recent(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pick the most important items for the AI payload.

    "Importance" here = recency: prefer items published within the last 24h,
    newest first, capped at 3. If nothing falls inside the 24h window (weekend,
    thinly-covered symbol) fall back to the most recent items available so the
    AI is not starved of context.
    """

    def _pub(item: dict[str, Any]) -> datetime:
        value = item.get("publishedAt")
        if not isinstance(value, datetime):
            return datetime.min.replace(tzinfo=UTC)
        # SQLite (and some feeds) hand back naive datetimes — treat as UTC so
        # the comparison against an aware cutoff never raises.
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    ordered = sorted(items, key=_pub, reverse=True)
    cutoff = datetime.now(UTC) - _PAYLOAD_WINDOW
    recent = [item for item in ordered if _pub(item) >= cutoff]
    selected = recent or ordered
    return selected[:_PAYLOAD_TOP_N]


def _serialize_item(item: dict[str, Any]) -> dict[str, Any]:
    published = item.get("publishedAt")
    return {
        "title": item["title"],
        "content": item.get("content"),
        "source": item.get("source"),
        "url": item.get("url"),
        "publishedAt": published.isoformat()
        if isinstance(published, datetime)
        else published,
    }


# ── RSS fetch + parse ────────────────────────────────────────────────────────────


async def _fetch_rss(symbol: str) -> list[dict[str, Any]]:
    query = f'"{symbol}" (hisse OR BIST OR "Borsa İstanbul")'
    url = _RSS_URL.format(query=quote(query))
    timeout = aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            body = await resp.text()
    return _filter_relevant_items(symbol, _parse_rss(body))[:_MAX_ITEMS_PER_SYMBOL]


def _filter_relevant_items(
    symbol: str, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep only headlines that name the requested BIST symbol explicitly."""
    normalized = symbol.strip().upper()
    if not normalized:
        return []
    symbol_pattern = re.compile(
        rf"(?<![A-Z0-9]){re.escape(normalized)}(?![A-Z0-9])",
        re.IGNORECASE,
    )
    relevant: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        source = str(item.get("source") or "").strip()
        source_suffix = f" - {source}" if source else ""
        if source_suffix and title.casefold().endswith(source_suffix.casefold()):
            title = title[: -len(source_suffix)].rstrip()
        if symbol_pattern.search(title):
            relevant.append(item)
        else:
            logger.debug(
                "Discarding unrelated news headline symbol=%s title=%s",
                normalized,
                str(item.get("title") or "")[:160],
            )
    return relevant


def _parse_rss(xml_text: str) -> list[dict[str, Any]]:
    """Parse a Google News RSS feed into title/summary/url/source/publishedAt items.

    Never raises on malformed individual items — a bad <item> is skipped,
    not fatal to the rest of the feed. The ``<description>`` field is HTML
    (a snippet / list of related links); we strip tags to a plain-text
    summary stored under ``content``.
    """
    items: list[dict[str, Any]] = []
    root = ET.fromstring(xml_text)
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        link = (item.findtext("link") or "").strip() or None
        source_el = item.find("source")
        source = (
            source_el.text.strip() if source_el is not None and source_el.text else None
        )
        summary = _strip_html(item.findtext("description"))
        items.append(
            {
                "title": title,
                # RSS aşamasında content = temizlenmiş özet; tam metin varsa
                # sonradan _enrich_with_fulltext üzerine yazar.
                "content": summary or None,
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


# ── HTML temizleme + tam metin çekimi ───────────────────────────────────────────


def _strip_html(raw: str | None) -> str:
    """Strip tags/entities from an HTML fragment, returning collapsed plain text.

    Robust against script/style blocks and malformed markup — used for both the
    RSS ``<description>`` snippet and full article bodies. Never raises.
    """
    if not raw:
        return ""
    text = _SCRIPT_STYLE_RE.sub(" ", raw)
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


async def _enrich_with_fulltext(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Best-effort: replace the RSS summary with real article text where possible.

    Bounded to the first ``_FULLTEXT_TOP_N`` items and only runs on a cache
    miss, so the hot scan path stays cheap. Any per-article failure keeps the
    existing summary — full text is a "nice to have", never required.
    """
    if not _FULLTEXT_ENABLED or not items:
        return items

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=_FULLTEXT_TIMEOUT_SECONDS),
        headers={"User-Agent": "Mozilla/5.0 (compatible; trade-ai-server/1.0)"},
    ) as session:
        tasks = [
            _fetch_article_text(session, item.get("url"))
            for item in items[:_FULLTEXT_TOP_N]
        ]
        bodies = await asyncio.gather(*tasks, return_exceptions=True)

    for item, body in zip(items, bodies):
        if isinstance(body, str) and len(body) > len(item.get("content") or ""):
            item["content"] = body[:_FULLTEXT_MAX_CHARS]
    return items


async def _fetch_article_text(session: aiohttp.ClientSession, url: str | None) -> str:
    """Fetch one article URL and return cleaned plain text (may be empty)."""
    if not url:
        return ""
    if not await _is_safe_public_http_url(url):
        logger.warning("Blocked unsafe full-text URL")
        return ""
    try:
        async with session.get(url, allow_redirects=False) as resp:
            if resp.status != 200:
                return ""
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype and "xml" not in ctype:
                return ""
            body = await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError):
        return ""
    except Exception:  # noqa: BLE001 — third-party HTML is unpredictable; never fatal
        logger.debug("Full-text fetch failed url=%s", url, exc_info=True)
        return ""
    return _strip_html(body)


async def _is_safe_public_http_url(url: str) -> bool:
    """Reject local/private/link-local/metadata/file targets before fetching."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname in {"localhost", "metadata.google.internal"} or hostname.endswith(
            (".localhost", ".local")
        ):
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        infos = await asyncio.get_running_loop().getaddrinfo(
            hostname, port, type=socket.SOCK_STREAM
        )
        addresses = {info[4][0] for info in infos}
        if not addresses:
            return False
        return all(ipaddress.ip_address(address).is_global for address in addresses)
    except (OSError, ValueError):
        return False
