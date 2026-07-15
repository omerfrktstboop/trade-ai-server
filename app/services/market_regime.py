"""Market regime service — BIST endeksinden makro piyasa rejimi.

Gateway'in endeks sembolü (default ``XU100``) snapshot'ından piyasa geneli
rejimi çıkarır. C# tarafı sembol bazında ``ClassifyMarketRegime`` hesaplar
(``HIGH_VOLATILITY`` / ``TRENDING`` / ``RANGE_LOW_VOLATILITY`` / ``NEUTRAL``);
biz bunun üstüne ayı-piyasası tespitini ekleriz: endeks fiyatı EMA20 ve
EMA50'nin altında + EMA20 < EMA50 (death-cross dizilimi) → ``DOWNTREND``.

Dönen rejim RiskEngine'in makro filtresine girer: ``DOWNTREND`` yeni BUY'ları
bloklar, ``HIGH_VOLATILITY`` güven eşiğini sertleştirir. SELL hiçbir rejimde
bloklanmaz — pozisyondan çıkış her koşulda serbesttir.

Fail-open: endeks verisi alınamazsa ``UNKNOWN`` döner ve makro filtre devreye
girmez — endeks feed'i kesildi diye tüm sistemi durdurmak yanlış pozitif
üretir; asıl güvenlik katmanları (RiskEngine'in diğer kuralları + gateway
limitleri) zaten ayakta.

60 sn'lik süreç içi cache ile her sembol değerlendirmesinde gateway'e ayrı
endeks çağrısı yapılmaz (scanner dakikada onlarca sembol tarayabilir).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import settings
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    MatriksGatewayClient,
    gateway_client,
)

logger = logging.getLogger(__name__)

_CACHE_TTL = timedelta(seconds=60)

# Makro rejim etiketleri (C# ClassifyMarketRegime çıktıları + DOWNTREND + UNKNOWN)
REGIME_UNKNOWN = "UNKNOWN"
REGIME_DOWNTREND = "DOWNTREND"
REGIME_HIGH_VOLATILITY = "HIGH_VOLATILITY"

_cached_regime: str = REGIME_UNKNOWN
_cached_at: datetime | None = None


async def get_index_regime(
    gateway: MatriksGatewayClient | None = None,
) -> str:
    """Endeks rejimini döndür (60 sn cache'li). Hata → ``UNKNOWN``."""
    global _cached_regime, _cached_at

    now = datetime.now(UTC)
    if _cached_at is not None and (now - _cached_at) < _CACHE_TTL:
        return _cached_regime

    gw = gateway or gateway_client
    symbol = settings.market_index_symbol.strip().upper()
    if not symbol:
        return REGIME_UNKNOWN

    try:
        snapshot = await gw.get_snapshot(symbol)
        payload = snapshot.get("payload") or {}
        regime = _classify(payload)
    except (GatewayUnavailable, GatewayError) as exc:
        logger.debug("Index snapshot unavailable symbol=%s error=%s", symbol, exc)
        regime = REGIME_UNKNOWN
    except Exception:
        logger.exception("Index regime classification crashed symbol=%s", symbol)
        regime = REGIME_UNKNOWN

    _cached_regime = regime
    _cached_at = now
    return regime


def _classify(payload: dict[str, Any]) -> str:
    """Endeks payload'ından makro rejim çıkar.

    Öncelik sırası:
    1. DOWNTREND — fiyat < EMA20 < EMA50 dizilimi (ayı piyasası). Gateway'in
       kendi ``marketRegime``'i trend yönü bilmez, bu yüzden burada türetilir.
    2. Gateway'in ``marketRegime`` alanı (HIGH_VOLATILITY vb.) aynen geçer.
    """
    last_price = _to_float(payload.get("lastPrice"))
    ema20 = _to_float(payload.get("ema20"))
    ema50 = _to_float(payload.get("ema50"))

    if (
        last_price is not None
        and ema20 is not None
        and ema50 is not None
        and last_price > 0
        and last_price < ema20 < ema50
    ):
        return REGIME_DOWNTREND

    regime = (
        str(
            payload.get("symbolTrendRegime")
            or payload.get("marketRegime")  # deprecated technical-features-v1 alias
            or ""
        )
        .strip()
        .upper()
    )
    return regime or REGIME_UNKNOWN


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def reset_cache() -> None:
    """Test yardımcıları için cache'i sıfırla."""
    global _cached_regime, _cached_at
    _cached_regime = REGIME_UNKNOWN
    _cached_at = None
