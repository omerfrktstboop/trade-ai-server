"""Tests for the token-cost decision gates (pre-flight + decision cache)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.decision_gate import (
    DecisionCache,
    preflight_wait_reason,
)


def _news(symbol: str = "THYAO", *, titles=(), hours_ago: float = 1.0, kap=()):
    published = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    return {
        symbol: {
            "latestNews": [{"title": t, "publishedAt": published} for t in titles],
            "kapNews": list(kap),
            "sentiment": "UNKNOWN",
        }
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Pre-flight gate
# ═══════════════════════════════════════════════════════════════════════════════


class TestPreflight:
    def test_neutral_no_news_no_position_gates_to_wait(self):
        reason = preflight_wait_reason(
            symbol="THYAO",
            indicator_consensus="NEUTRAL",
            bot_position_qty=0.0,
            news_context=_news(titles=()),
        )
        assert reason is not None
        assert "NEUTRAL" in reason

    def test_non_neutral_consensus_goes_to_llm(self):
        assert (
            preflight_wait_reason(
                symbol="THYAO",
                indicator_consensus="BUY",
                bot_position_qty=0.0,
                news_context=_news(titles=()),
            )
            is None
        )

    def test_missing_consensus_goes_to_llm(self):
        """Veri yokluğu nötrlük kanıtı değildir — kapı devreye girmez."""
        assert (
            preflight_wait_reason(
                symbol="THYAO",
                indicator_consensus=None,
                bot_position_qty=0.0,
                news_context=_news(titles=()),
            )
            is None
        )

    def test_open_position_goes_to_llm(self):
        """Açık pozisyonda çıkış kararı gerekebilir — LLM devrede kalır."""
        assert (
            preflight_wait_reason(
                symbol="THYAO",
                indicator_consensus="NEUTRAL",
                bot_position_qty=10.0,
                news_context=_news(titles=()),
            )
            is None
        )

    def test_fresh_news_goes_to_llm(self):
        assert (
            preflight_wait_reason(
                symbol="THYAO",
                indicator_consensus="NEUTRAL",
                bot_position_qty=0.0,
                news_context=_news(titles=("THYAO rekor kar",), hours_ago=1),
            )
            is None
        )

    def test_stale_news_still_gates(self):
        """24 saatten eski haber 'taze' değildir — kapı çalışır."""
        reason = preflight_wait_reason(
            symbol="THYAO",
            indicator_consensus="NEUTRAL",
            bot_position_qty=0.0,
            news_context=_news(titles=("Eski haber",), hours_ago=30),
        )
        assert reason is not None

    def test_kap_news_goes_to_llm(self):
        assert (
            preflight_wait_reason(
                symbol="THYAO",
                indicator_consensus="NEUTRAL",
                bot_position_qty=0.0,
                news_context=_news(titles=(), kap=("KAP bildirimi",)),
            )
            is None
        )

    def test_undated_news_treated_as_fresh(self):
        """publishedAt eksikse temkinli davran: taze say, LLM'e git."""
        ctx = {
            "THYAO": {
                "latestNews": [{"title": "Tarihi belirsiz haber"}],
                "kapNews": [],
            }
        }
        assert (
            preflight_wait_reason(
                symbol="THYAO",
                indicator_consensus="NEUTRAL",
                bot_position_qty=0.0,
                news_context=ctx,
            )
            is None
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Decision cache
# ═══════════════════════════════════════════════════════════════════════════════


class TestDecisionCache:
    def _raw(self, action="WAIT", confidence=50.0):
        return {"action": action, "confidence": confidence, "reason": "llm says"}

    def test_hit_within_ttl_and_drift(self):
        cache = DecisionCache()
        news = _news(titles=("A",))
        cache.put("THYAO", 100.0, news, self._raw())

        hit = cache.get("THYAO", 100.5, news)  # %0.5 drift

        assert hit is not None
        assert hit["action"] == "WAIT"
        assert "cached decision" in hit["reason"]

    def test_miss_on_price_drift_above_1pct(self):
        cache = DecisionCache()
        news = _news(titles=("A",))
        cache.put("THYAO", 100.0, news, self._raw())

        assert cache.get("THYAO", 101.5, news) is None  # %1.5 drift

    def test_miss_on_new_news(self):
        cache = DecisionCache()
        cache.put("THYAO", 100.0, _news(titles=("A",)), self._raw())

        assert cache.get("THYAO", 100.0, _news(titles=("A", "B"))) is None

    def test_miss_after_ttl(self):
        cache = DecisionCache(ttl=timedelta(seconds=0))
        news = _news(titles=("A",))
        cache.put("THYAO", 100.0, news, self._raw())

        assert cache.get("THYAO", 100.0, news) is None

    def test_miss_on_unknown_symbol(self):
        cache = DecisionCache()
        assert cache.get("AKBNK", 100.0, None) is None

    def test_cached_copy_is_isolated(self):
        """Cache'ten dönen dict'i mutasyonlamak cache'i bozmamalı."""
        cache = DecisionCache()
        news = _news(titles=("A",))
        cache.put("THYAO", 100.0, news, self._raw())

        first = cache.get("THYAO", 100.0, news)
        first["action"] = "BUY"

        second = cache.get("THYAO", 100.0, news)
        assert second["action"] == "WAIT"

    def test_symbol_normalized(self):
        cache = DecisionCache()
        news = _news(titles=("A",))
        cache.put("thyao", 100.0, news, self._raw())

        assert cache.get("THYAO", 100.0, news) is not None
