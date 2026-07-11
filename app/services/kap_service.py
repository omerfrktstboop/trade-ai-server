"""Matriks KAP cache normalization and AI context (never sends orders)."""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.db import KapEvent
from app.services.matriks_gateway import GatewayError, GatewayUnavailable, gateway_client

logger = logging.getLogger(__name__)
KAP_GATEWAY_CACHE_TTL_SECONDS = 120
_kap_gateway_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def invalidate_kap_cache(symbol: str | None = None) -> None:
    """Invalidate one symbol after a risk-news signal, or all symbols."""
    if symbol:
        _kap_gateway_cache.pop(symbol.strip().upper(), None)
    else:
        _kap_gateway_cache.clear()

_RISK_KEYWORDS = {
    "BLOCKING": ("tedbir", "brüt takas", "brut takas", "kredili işlem yasağı", "açığa satış yasağı", "aciga satis yasagi", "faaliyet durdurma", "konkordato", "iflas", "haciz"),
    "HIGH": ("spk inceleme", "dava", "ceza", "bilanço zararı", "bilanco zarari"),
    "MEDIUM": ("bedelli sermaye artırımı", "bedelli sermaye artirimi", "pay satışı", "pay satisi", "ortak satışı", "ortak satisi"),
}


def _value(raw: dict[str, Any], *names: str) -> Any:
    lowered = {str(key).casefold(): value for key, value in raw.items()}
    for name in names:
        if name.casefold() in lowered:
            return lowered[name.casefold()]
    return None


def _published(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _is_active_risk(published_at: datetime | None, *, now: datetime, lookback_hours: int) -> bool:
    """Unknown dates remain auditable but cannot create an unbounded live lock."""
    if published_at is None:
        return False
    normalized = published_at if published_at.tzinfo else published_at.replace(tzinfo=UTC)
    return normalized.astimezone(UTC) >= now.astimezone(UTC) - timedelta(hours=lookback_hours)


def classify_kap(title: str, content: str | None) -> tuple[str, str]:
    text = f"{title} {content or ''}".casefold()
    for level, keywords in _RISK_KEYWORDS.items():
        if any(keyword.casefold() in text for keyword in keywords):
            if "brüt takas" in text or "brut takas" in text:
                return "BRUT_TAKAS", level
            if "tedbir" in text or "yasak" in text:
                return "REGULATORY_MEASURE", level
            if "bedelli" in text:
                return "CAPITAL_INCREASE", level
            if "satış" in text or "satis" in text:
                return "SHARE_SALE", level
            if "dava" in text or "ceza" in text:
                return "LEGAL_CASE", level
            return "MATERIAL_DISCLOSURE", level
    if "temettü" in text or "temettu" in text or "dividend" in text: return "DIVIDEND", "LOW"
    if "finansal" in text or "bilanço" in text or "bilanco" in text: return "FINANCIAL_STATEMENT", "LOW"
    if "ilişkili taraf" in text or "iliskili taraf" in text: return "RELATED_PARTY", "LOW"
    return "UNKNOWN", "LOW"


async def sync_kap_events(symbol: str, limit: int = 50) -> list[KapEvent]:
    symbol = symbol.strip().upper()
    try:
        cached = _kap_gateway_cache.get(symbol)
        if cached and time.monotonic() - cached[0] < KAP_GATEWAY_CACHE_TTL_SECONDS:
            payload = cached[1]
        else:
            payload = await gateway_client.get_kap(symbol, limit)
            _kap_gateway_cache[symbol] = (time.monotonic(), payload)
    except (GatewayUnavailable, GatewayError):
        return []
    entries = payload.get("news") or payload.get("events") or []
    if not isinstance(entries, list):
        return []
    created: list[KapEvent] = []
    try:
        async with async_session_factory() as session:
            for raw in entries:
                if not isinstance(raw, dict): continue
                title = str(_value(raw, "title", "header", "headline") or "").strip()
                if not title: continue
                content = _value(raw, "content", "body", "description", "text")
                content = str(content) if content is not None else None
                published_at = _published(_value(raw, "publishedAt", "published_at", "timestamp", "date", "datetime", "DateTime"))
                existing = select(KapEvent).where(KapEvent.symbol == symbol, KapEvent.title == title)
                existing = existing.where(KapEvent.published_at.is_(None) if published_at is None else KapEvent.published_at == published_at)
                if (await session.execute(existing)).scalar_one_or_none() is not None: continue
                event_type, risk_level = classify_kap(title, content)
                row = KapEvent(symbol=symbol, title=title, content=content, event_type=event_type, risk_level=risk_level, published_at=published_at, source=str(payload.get("source") or "MATRIKS_NEWS_FALLBACK"), raw_json=raw)
                session.add(row); created.append(row)
            await session.commit()
    except Exception:
        logger.exception("KAP persistence failed symbol=%s", symbol)
        return []
    return created


async def get_kap_context(symbols: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for symbol_raw in symbols:
        symbol = symbol_raw.strip().upper()
        if not symbol: continue
        await sync_kap_events(symbol)
        try:
            async with async_session_factory() as session:
                rows = list((await session.execute(select(KapEvent).where(KapEvent.symbol == symbol).order_by(KapEvent.published_at.desc().nullslast()).limit(10))).scalars().all())
                # Unknown dates stay visible for audit but never become an
                # unbounded live BUY lock. Only dated events enter the window.
                now = datetime.now(UTC)
                risk_rows = [row for row in rows if row.risk_level in {"HIGH", "BLOCKING"} and _is_active_risk(row.published_at, now=now, lookback_hours=24)]
        except Exception:
            result[symbol] = {"latestEvents": [], "riskEvents24h": [], "hasBlockingRisk": False, "summary": "KAP unavailable"}; continue
        serialize = lambda row: {"title": row.title, "eventType": row.event_type, "riskLevel": row.risk_level, "publishedAt": row.published_at.isoformat() if row.published_at else None, "dateUnknown": row.published_at is None, "source": row.source}
        important_rows = sorted(rows, key=lambda row: (row.risk_level in {"BLOCKING", "HIGH"}, row.published_at or datetime.min.replace(tzinfo=UTC)), reverse=True)[:5]
        newest = max((row.published_at for row in rows if row.published_at), default=None)
        kap_age = max(0.0, (datetime.now(UTC) - newest).total_seconds()) if newest else None
        result[symbol] = {"latestEvents": [serialize(row) for row in important_rows], "riskEvents24h": [serialize(row) for row in risk_rows], "hasBlockingRisk": any(row.risk_level == "BLOCKING" for row in risk_rows), "kapAgeSeconds": kap_age, "summary": f"{len(rows)} KAP events, {len(risk_rows)} elevated risk events"}
    return result
