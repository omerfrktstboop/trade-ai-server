from __future__ import annotations
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from app.config import settings
from app.db.session import async_session_factory
from app.models.db import NewsCache

async def active_news_risk(symbol: str) -> tuple[str, str] | None:
    if not settings.news_risk_lock_enabled or not settings.news_risk_buy_block_enabled: return None
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.news_risk_lookback_hours)
        async with async_session_factory() as session:
            rows = list((await session.execute(select(NewsCache).where(NewsCache.symbol == symbol.upper(), NewsCache.cached_at >= cutoff))).scalars().all())
        for row in rows:
            text = f"{row.title} {row.content or ''}".casefold()
            for keyword in (x.strip() for x in settings.news_risk_keywords_csv.split(",") if x.strip()):
                if keyword.casefold() in text: return keyword, row.title
    except Exception:
        return None
    return None
