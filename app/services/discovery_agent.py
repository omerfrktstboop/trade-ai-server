"""Discovery agent — movers tabanlı otonom hisse keşfi.

Gateway'in ``GET /movers`` endpoint'inden günlük yükselen / düşen / hacimli
sembolleri alır, tuzak adayları Python tarafında eler ve kalanları dinamik
``watchlist_symbols`` tablosuna yazar. Scanner bu tabloyu her tick'te okuyup
aktif watchlist sembollerini normal analiz döngüsüne katar.

Eleme kuralları (her biri ``settings``ten ayarlanır):

1. **Tavan/taban kilidi** — |changePct| >= ``discovery_ceiling_change_pct``
   (default %9.5). Tavan kitlemiş hissede emir zaten geçmez; taban kitlemiş
   hisse bıçak yakalamaktır.
2. **Sığ hacim** — günlük hacim (TL) < ``discovery_min_volume_tl``.
   Sığ kağıtta hem spread maliyeti yüksek hem manipülasyon riski var.
3. **Satış duvarı** — derinlikte toplam ask hacmi / toplam bid hacmi >
   ``discovery_max_ask_bid_ratio``. Yukarısı satış emirleriyle örülmüş
   kağıda yeni pozisyon anlamsız.

Watchlist'e YAZMAK sembolü işlem evrenine SOKMAZ: emir yolu RiskEngine'in
``allowedSymbols`` kontrolünden geçmeye devam eder. Watchlist yalnızca
"scanner bunları da analiz etsin" listesidir; gateway_config bu sembolleri
data-only olarak gateway aboneliğine ekler.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from app.config import settings
from app.db.session import async_session_factory
from app.models.db import WatchlistSymbol
from app.models.db import WatchlistQualityScore
from app.services.watchlist_quality import calculate_quality
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    MatriksGatewayClient,
    gateway_client,
)

logger = logging.getLogger(__name__)

# Bu süre boyunca movers'ta hiç görünmeyen watchlist kaydı pasifleştirilir.
_STALE_AFTER = timedelta(hours=24)


@dataclass(frozen=True)
class DiscoveryVerdict:
    reason: str
    wall_ratio: float | None


async def run_discovery_scan(
    gateway: MatriksGatewayClient | None = None,
) -> list[str]:
    """Movers'ı çek, ele, kalan adayları watchlist'e yaz.

    Returns:
        Bu turda watchlist'e eklenen/tazelenen sembollerin listesi.
        Gateway/movers kullanılamıyorsa boş liste (fail-open).
    """
    gw = gateway or gateway_client

    try:
        movers = await gw.get_movers()
    except (GatewayUnavailable, GatewayError) as exc:
        logger.debug("Movers unavailable: %s", exc)
        return []

    if not movers.get("available"):
        return []

    items = {
        str(item.get("symbol") or "").upper(): item
        for item in movers.get("items") or []
        if item.get("symbol")
    }

    # Aday havuzu: üç listenin birleşimi, kaynağıyla birlikte.
    candidates: dict[str, str] = {}
    for source, key in (
        ("GAINER", "gainers"),
        ("LOSER", "losers"),
        ("VOLUME_LEADER", "volumeLeaders"),
    ):
        for symbol in movers.get(key) or []:
            candidates.setdefault(str(symbol).upper(), source)

    accepted: list[tuple[str, str, dict[str, Any], str, dict[str, Any]]] = []
    for symbol, source in candidates.items():
        item = items.get(symbol)
        if item is None:
            continue
        verdict = await _screen(gw, symbol, item)
        if verdict is None:
            continue  # elendi
        quality = calculate_quality(item, verdict.wall_ratio)
        if quality["quality"] < settings.watchlist_min_quality_score:
            logger.debug("Discovery reject %s: quality %.1f", symbol, quality["quality"])
            continue
        accepted.append((symbol, source, item, verdict.reason, quality))

    if accepted:
        await _upsert_watchlist(accepted)
    await _deactivate_stale()

    return [symbol for symbol, *_ in accepted]


async def _screen(
    gw: MatriksGatewayClient, symbol: str, item: dict[str, Any]
) -> DiscoveryVerdict | None:
    """Aday elemeleri. Geçerse kabul gerekçesi (str), elenirse None."""
    change_pct = _to_float(item.get("changePct")) or 0.0
    volume = _to_float(item.get("volume")) or 0.0

    # 1. Tavan/taban kilidi.
    if abs(change_pct) >= settings.discovery_ceiling_change_pct:
        logger.debug(
            "Discovery reject %s: limit-locked changePct=%.2f", symbol, change_pct
        )
        return None

    # 2. Sığ hacim.
    if volume < settings.discovery_min_volume_tl:
        logger.debug(
            "Discovery reject %s: thin volume %.0f < %.0f",
            symbol, volume, settings.discovery_min_volume_tl,
        )
        return None

    # 3. Satış duvarı (derinlik) — derinlik alınamazsa bu filtre atlanır;
    #    kalan iki filtreyi geçen aday derinliksiz de kabul edilir.
    try:
        depth = await gw.get_depth(symbol)
        wall_ratio = _ask_bid_ratio(depth)
        analysis = depth.get("depthAnalysis") or depth.get("analysis") or {}
        if analysis.get("orderBookSignal") == "STRONG_SELL_PRESSURE":
            logger.debug("Discovery reject %s: strong sell pressure", symbol)
            return None
        spread_pct = _to_float(analysis.get("spreadPct"))
        if spread_pct is not None and spread_pct > 0.50:
            logger.debug("Discovery reject %s: spread %.2f%%", symbol, spread_pct)
            return None
        if wall_ratio is not None and wall_ratio > settings.discovery_max_ask_bid_ratio:
            logger.debug(
                "Discovery reject %s: sell wall ask/bid=%.2f", symbol, wall_ratio
            )
            return None
    except (GatewayUnavailable, GatewayError):
        wall_ratio = None
    except Exception:  # noqa: BLE001
        logger.debug("Depth screen failed symbol=%s", symbol, exc_info=True)
        wall_ratio = None

    parts = [f"changePct={change_pct:+.2f}", f"volumeTl={volume:,.0f}"]
    if wall_ratio is not None:
        parts.append(f"askBidRatio={wall_ratio:.2f}")
    return DiscoveryVerdict(reason="; ".join(parts), wall_ratio=wall_ratio)


def _ask_bid_ratio(depth: dict[str, Any]) -> float | None:
    """Toplam ask hacmi / toplam bid hacmi. Veri yoksa None."""
    payload = depth.get("payload") or depth
    analysis = payload.get("depthAnalysis") or payload.get("analysis") or {}
    bid_ask = _to_float(analysis.get("bidAskRatioTop25"))
    if bid_ask is not None and bid_ask > 0:
        return 1.0 / bid_ask
    bids = payload.get("bids") or []
    asks = payload.get("asks") or []
    total_bid = sum(_to_float(level.get("size")) or 0.0 for level in bids)
    total_ask = sum(_to_float(level.get("size")) or 0.0 for level in asks)
    if total_bid <= 0 or total_ask <= 0:
        return None
    return total_ask / total_bid


async def _upsert_watchlist(
    accepted: list[tuple[str, str, dict[str, Any], str, dict[str, Any]]]
) -> None:
    try:
        async with async_session_factory() as session:
            for symbol, source, item, reason, quality in accepted:
                row = (
                    await session.execute(
                        select(WatchlistSymbol).where(WatchlistSymbol.symbol == symbol)
                    )
                ).scalar_one_or_none()
                if row is None:
                    session.add(
                        WatchlistSymbol(
                            symbol=symbol,
                            source=source,
                            reason=reason,
                            change_pct=_to_float(item.get("changePct")),
                            volume=_to_float(item.get("volume")),
                            is_active=True,
                        )
                    )
                    logger.info(
                        "Watchlist add symbol=%s source=%s %s", symbol, source, reason
                    )
                else:
                    row.source = source
                    row.reason = reason
                    row.change_pct = _to_float(item.get("changePct"))
                    row.volume = _to_float(item.get("volume"))
                    row.is_active = True
                    row.last_seen_at = datetime.now(UTC)
                score = (await session.execute(select(WatchlistQualityScore).where(WatchlistQualityScore.symbol == symbol))).scalar_one_or_none()
                values = {"quality_score": quality["quality"], "momentum_score": quality["momentum"], "volume_score": quality["volume"], "depth_score": quality["depth"], "news_score": quality["news"], "risk_score": quality["risk"], "reason_json": quality}
                if score is None:
                    session.add(WatchlistQualityScore(symbol=symbol, **values))
                else:
                    for key, value in values.items():
                        setattr(score, key, value)
            await session.commit()
    except Exception:
        logger.exception("Watchlist upsert failed")


async def _deactivate_stale() -> None:
    """Uzun süredir movers'ta görünmeyen kayıtları pasifleştir."""
    try:
        cutoff = datetime.now(UTC) - _STALE_AFTER
        async with async_session_factory() as session:
            await session.execute(
                update(WatchlistSymbol)
                .where(
                    WatchlistSymbol.is_active.is_(True),
                    WatchlistSymbol.last_seen_at < cutoff,
                )
                .values(is_active=False)
            )
            await session.commit()
    except Exception:
        logger.exception("Watchlist stale-deactivation failed")


async def list_active_watchlist_symbols() -> list[str]:
    """Scanner'ın tarama listesine katılacak aktif watchlist sembolleri."""
    try:
        async with async_session_factory() as session:
            rows = (
                await session.execute(
                    select(WatchlistSymbol.symbol).where(
                        WatchlistSymbol.is_active.is_(True)
                    )
                )
            ).scalars().all()
        return [str(s).upper() for s in rows]
    except Exception:
        logger.exception("Watchlist read failed")
        return []


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
