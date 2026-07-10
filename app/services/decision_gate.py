"""Decision gate — token-cost optimizasyonu için LLM öncesi kapılar.

İki mekanizma, ikisi de LLM çağrısını tamamen atlatır:

1. **Pre-flight (rule-based gating).** Gateway'in kendi indikatör konsensüsü
   ``NEUTRAL`` ise, sembol için taze haber yoksa ve yönetilecek açık bot
   pozisyonu da yoksa, LLM'in vereceği tek makul karar zaten WAIT'tir —
   sormaya gerek yok. Açık pozisyon varken kapı devre dışı kalır: çıkış
   (stop/target) kararları her zaman LLM'e gider.

2. **Karar cache'i (zaman/fiyat/haber duyarlı).** Aynı sembol için son
   ``_CACHE_TTL`` içinde LLM'e sorulmuş, fiyat o karardan bu yana %1'den az
   oynamış ve haber seti değişmemişse önceki ham karar aynen tekrarlanır.
   Cache YALNIZCA gerçek LLM cevaplarını saklar (pre-flight WAIT'leri değil)
   ve süreç içi (in-memory) yaşar — restart'ta temiz başlar.

Her iki kapı da fail-open'dır: beklenmedik veri şekli → kapı devreye girmez,
LLM çağrısı normal yoluna devam eder. Kapılar asla BUY/SELL üretmez; yalnızca
"LLM'e sormadan WAIT dön" ya da "önceki kararı tekrarla" diyebilir.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# Pre-flight: "taze haber" penceresi — bu pencerede yayınlanmış haber varsa
# NEUTRAL konsensüste bile LLM'e sorulur (haber teknik resmi kırabilir).
_FRESH_NEWS_WINDOW = timedelta(hours=12)

# Cache parametreleri.
_CACHE_TTL = timedelta(hours=1)
_CACHE_MAX_PRICE_DRIFT = 0.01  # %1


# ── Pre-flight (rule-based gating) ──────────────────────────────────────────────


def preflight_wait_reason(
    *,
    symbol: str,
    indicator_consensus: str | None,
    bot_position_qty: float,
    news_context: dict[str, Any] | None,
) -> str | None:
    """LLM'siz WAIT gerekçesi döndür; kapı uygulanamıyorsa None.

    Koşullar (hepsi birden):
    - Gateway konsensüsü kesin olarak ``NEUTRAL`` (eksik/None ise kapı yok —
      veri yokluğu nötrlük kanıtı değildir),
    - Sembol için taze haber yok (KAP veya son 12 saatte yayınlanmış başlık),
    - Yönetilecek açık bot pozisyonu yok.
    """
    if (indicator_consensus or "").strip().upper() != "NEUTRAL":
        return None
    if bot_position_qty > 0:
        # Açık pozisyon = çıkış kararı gerekebilir; LLM devrede kalmalı.
        return None
    if _has_fresh_news(symbol, news_context):
        return None
    return (
        "Pre-flight gate: indicator consensus NEUTRAL, no fresh news, "
        "no open position — WAIT without LLM call."
    )


def _has_fresh_news(symbol: str, news_context: dict[str, Any] | None) -> bool:
    """Sembol için KAP haberi veya taze başlık var mı? Şüphede: var (True).

    Fail-open: haber yapısı beklenmedikse "haber var" deriz ki kapı kapanmasın
    ve karar LLM'e gitsin.
    """
    if not news_context:
        return False
    entry = news_context.get(symbol.strip().upper())
    if not isinstance(entry, dict):
        return False
    try:
        if entry.get("kapNews"):
            return True
        cutoff = datetime.now(UTC) - _FRESH_NEWS_WINDOW
        for item in entry.get("latestNews") or []:
            published_raw = item.get("publishedAt")
            if not published_raw:
                # Tarihi bilinmeyen haber: temkinli davran, taze say.
                return True
            published = _parse_dt(published_raw)
            if published is None or published >= cutoff:
                return True
    except Exception:  # noqa: BLE001 — kapı asla evaluation'ı düşürmemeli
        logger.debug("Fresh-news check failed symbol=%s", symbol, exc_info=True)
        return True
    return False


def _parse_dt(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


# ── Karar cache'i ────────────────────────────────────────────────────────────────


@dataclass
class _CacheEntry:
    raw: dict[str, Any]
    last_price: float
    news_fingerprint: tuple[str, ...]
    cached_at: datetime


class DecisionCache:
    """Sembol başına son LLM kararını fiyat/haber şartıyla tekrar kullanır."""

    def __init__(
        self,
        ttl: timedelta = _CACHE_TTL,
        max_price_drift: float = _CACHE_MAX_PRICE_DRIFT,
    ) -> None:
        self._ttl = ttl
        self._max_price_drift = max_price_drift
        self._entries: dict[str, _CacheEntry] = {}

    def get(
        self,
        symbol: str,
        last_price: float,
        news_context: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """TTL + fiyat + haber şartları sağlanıyorsa önceki ham kararı döndür."""
        entry = self._entries.get(symbol.strip().upper())
        if entry is None:
            return None
        if datetime.now(UTC) - entry.cached_at >= self._ttl:
            return None
        if entry.last_price <= 0 or last_price <= 0:
            return None
        drift = abs(last_price - entry.last_price) / entry.last_price
        if drift > self._max_price_drift:
            return None
        if _news_fingerprint(symbol, news_context) != entry.news_fingerprint:
            return None
        raw = dict(entry.raw)
        raw["reason"] = (
            f"{raw.get('reason', '')} [cached decision: price drift "
            f"{drift * 100:.2f}% < 1%, no new news, age "
            f"{(datetime.now(UTC) - entry.cached_at).seconds // 60}min]"
        ).strip()
        return raw

    def put(
        self,
        symbol: str,
        last_price: float,
        news_context: dict[str, Any] | None,
        raw: dict[str, Any],
    ) -> None:
        if last_price <= 0 or not isinstance(raw, dict):
            return
        self._entries[symbol.strip().upper()] = _CacheEntry(
            raw=dict(raw),
            last_price=last_price,
            news_fingerprint=_news_fingerprint(symbol, news_context),
            cached_at=datetime.now(UTC),
        )

    def clear(self) -> None:
        self._entries.clear()


def _news_fingerprint(
    symbol: str, news_context: dict[str, Any] | None
) -> tuple[str, ...]:
    """Haber setinin kimliği: başlıklar değişti mi? Sıra bağımsız."""
    if not news_context:
        return ()
    entry = news_context.get(symbol.strip().upper())
    if not isinstance(entry, dict):
        return ()
    titles: list[str] = []
    try:
        for item in entry.get("latestNews") or []:
            title = str(item.get("title") or "")
            if title:
                titles.append(title)
        for item in entry.get("kapNews") or []:
            titles.append(str(item))
    except Exception:  # noqa: BLE001
        return ("<unparseable>",)
    return tuple(sorted(titles))


# Modül seviyesinde paylaşılan tek cache — evaluator bunu kullanır.
decision_cache = DecisionCache()
