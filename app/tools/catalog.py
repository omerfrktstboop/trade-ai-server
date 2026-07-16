"""Whitelist read-only araç tanımları.

Her araç ``MatriksGatewayClient``'ın (veya DB'nin) ince bir sargısıdır.
``audience`` alanı ilke #8'i uygular:

- ``{"ai", "mcp"}`` — sembol kapsamlı veri araçları; DeepSeek değerlendirme
  sırasında SADECE değerlendirilen sembol (+ bağlı sembol) için çağırabilir.
- ``{"mcp"}`` — geniş hesap/portföy araçları; yalnızca admin MCP yüzeyi.

Bu modülde emir gönderme, emir iptali, config yazma veya kill switch aracı
TANIMLANAMAZ — testler bunu negatif assert ile korur.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.services import matriks_gateway
from app.tools.registry import tool

#: Ham hesap/kullanıcı kimliği taşıyabilecek anahtarlar — MCP/AI çıktısından
#: maskelenir. Bakiye/limit alanları aynen kalır.
_SENSITIVE_ACCOUNT_KEYS = {
    "accountid",
    "account",
    "accountno",
    "accountnumber",
    "tradeuserid",
    "userid",
    "username",
    "customerid",
    "customerno",
    "name",
    "fullname",
}


def _gateway() -> matriks_gateway.MatriksGatewayClient:
    """Modül-attribute indirection: testler ``matriks_gateway.gateway_client``
    yerine buradaki fonksiyonun döndürdüğü client'ı monkeypatch'leyebilsin."""
    return matriks_gateway.gateway_client


def _mask_value(value: Any) -> str:
    text = str(value)
    if len(text) <= 2:
        return "***"
    return f"{text[:2]}***"


def _mask_account_fields(payload: Any) -> Any:
    """Ham hesap kimliği alanlarını özyinelemeli maskele."""
    if isinstance(payload, dict):
        masked: dict[str, Any] = {}
        for key, value in payload.items():
            if key.replace("_", "").lower() in _SENSITIVE_ACCOUNT_KEYS and isinstance(
                value, (str, int)
            ):
                masked[key] = _mask_value(value)
            else:
                masked[key] = _mask_account_fields(value)
        return masked
    if isinstance(payload, list):
        return [_mask_account_fields(item) for item in payload]
    return payload


# ── Sembol kapsamlı araçlar (AI + MCP) ──────────────────────────────────────


@tool(
    "get_snapshot",
    "Sembol için anlık fiyat görüntüsü: OHLCV, son fiyat, temel göstergeler ve "
    "teknik feature bloğu.",
)
async def get_snapshot(symbol: str) -> dict[str, Any]:
    return await _gateway().get_snapshot(symbol)


@tool(
    "get_bars",
    "Sembol için OHLC bar geçmişi (gateway açılışından beri biriken seri; "
    "en fazla 250 bar).",
)
async def get_bars(symbol: str, count: int = 100) -> dict[str, Any]:
    return await _gateway().get_bars(symbol, count=max(1, min(250, count)))


@tool(
    "get_depth",
    "Sembol için emir defteri kademe verisi: alış/satış seviyeleri ve "
    "dengesizlik metrikleri (en fazla 25 kademe).",
)
async def get_depth(symbol: str, levels: int = 10) -> dict[str, Any]:
    return await _gateway().get_depth(symbol, levels=max(1, min(25, levels)))


@tool(
    "get_indicators",
    "Sembol için teknik göstergeler: RSI, MACD, EMA20/50, ATR/NATR "
    "(gateway destekliyorsa MOST ve ADX dahil).",
)
async def get_indicators(symbol: str) -> dict[str, Any]:
    return await _gateway().get_indicators(symbol)


@tool(
    "get_news",
    "Sembol için canlı Matriks haber başlıkları (gateway açılışından beri "
    "cache'lenen; en fazla 20).",
)
async def get_news(symbol: str, limit: int = 10) -> dict[str, Any]:
    return await _gateway().get_news(symbol, limit=max(1, min(20, limit)))


@tool(
    "get_kap",
    "Sembol için KAP bildirimleri (en fazla 20).",
)
async def get_kap(symbol: str, limit: int = 10) -> dict[str, Any]:
    return await _gateway().get_kap(symbol, limit=max(1, min(20, limit)))


@tool(
    "get_institutions",
    "Sembol için günlük AKD/kurum dağılımı: en büyük alıcı ve satıcı kurumlar "
    "(lisans gerektirir).",
)
async def get_institutions(symbol: str, limit: int = 5) -> dict[str, Any]:
    return await _gateway().get_institutions(symbol, limit=max(1, min(20, limit)))


@tool(
    "get_position",
    "Sembol için bot pozisyonu: lot, ortalama maliyet ve canlı fiyata göre "
    "gerçekleşmemiş K/Z yüzdesi.",
)
async def get_position(symbol: str) -> dict[str, Any]:
    from app.db.session import async_session_factory
    from app.models.db.bot_position import BotPosition

    normalized = symbol.strip().upper()
    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(BotPosition).where(BotPosition.symbol == normalized)
            )
        ).scalar_one_or_none()

    if row is None or (row.qty or 0) <= 0:
        return {"symbol": normalized, "qty": 0, "avgCost": None, "unrealizedPnlPct": None}

    last_price: float | None = None
    try:
        snapshot = await _gateway().get_snapshot(normalized)
        payload = snapshot.get("payload") if isinstance(snapshot, dict) else None
        raw_last = (payload or {}).get("lastPrice") if isinstance(payload, dict) else None
        if raw_last is None and isinstance(snapshot, dict):
            raw_last = snapshot.get("lastPrice")
        last_price = float(raw_last) if raw_last else None
    except Exception:
        last_price = None

    avg_cost = float(row.avg_price) if row.avg_price else None
    pnl_pct = (
        round((last_price - avg_cost) / avg_cost * 100.0, 2)
        if last_price and avg_cost
        else None
    )
    return {
        "symbol": normalized,
        "qty": row.qty,
        "avgCost": avg_cost,
        "lastPrice": last_price,
        "unrealizedPnlPct": pnl_pct,
    }


# ── Geniş araçlar (SADECE admin MCP — DeepSeek asla görmez) ─────────────────


@tool(
    "get_positions",
    "Tüm portföy: sembol başına bot lotu, kilitli uzun vade lotu ve toplam lot.",
    audience={"mcp"},
)
async def get_positions() -> dict[str, Any]:
    return await _gateway().get_positions()


@tool(
    "get_real_positions",
    "Borsadaki gerçek hesap pozisyonları (sembol anahtarlı).",
    audience={"mcp"},
)
async def get_real_positions() -> dict[str, Any]:
    return _mask_account_fields(await _gateway().get_real_positions())


@tool(
    "get_account_summary",
    "Hesap özeti: bakiye, kullanılabilir limit, toplam değer. Hesap kimliği "
    "maskelenir — ham id hiçbir tüketiciye dönmez.",
    audience={"mcp"},
)
async def get_account_summary() -> dict[str, Any]:
    return _mask_account_fields(await _gateway().get_account())


@tool(
    "get_movers",
    "Takip edilen evrendeki yerel hareketlilik sıralaması (BIST geneli değil).",
    audience={"mcp"},
)
async def get_movers(limit: int = 10) -> dict[str, Any]:
    return await _gateway().get_movers(limit=max(1, min(20, limit)))
