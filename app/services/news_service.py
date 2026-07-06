"""News service — provides news context for AI trading decisions.

Currently returns empty mock data. Future versions will scrape real sources
and cache results in the ``news_cache`` DB table.
"""

from __future__ import annotations

from typing import Any


# ── Public interface ───────────────────────────────────────────────────────────


async def get_news_context(symbols: list[str]) -> dict[str, Any]:
    """Return news context for a list of symbols.

    Args:
        symbols: List of trading symbols (e.g. ``["THYAO", "AKBNK"]``).

    Returns:
        Dict keyed by symbol, each with ``latestNews``, ``kapNews``, and
        ``sentiment``. Currently returns empty lists and ``UNKNOWN`` sentiment
        as a safe mock implementation.
    """
    news: dict[str, Any] = {}
    for symbol in symbols:
        news[symbol] = {
            "latestNews": [],
            "kapNews": [],
            "sentiment": "UNKNOWN",
        }
    return news
