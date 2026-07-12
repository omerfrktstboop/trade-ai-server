from __future__ import annotations
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from app.config import settings
from app.db.session import async_session_factory
from app.models.db import KapEvent, NewsCache
from app.models.signal import OrderType, SignalAction, SignalResponse


async def active_news_risk(symbol: str) -> tuple[str, str] | None:
    if not settings.news_risk_lock_enabled or not settings.news_risk_buy_block_enabled:
        return None
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=settings.news_risk_lookback_hours
        )
        async with async_session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(NewsCache).where(
                            NewsCache.symbol == symbol.upper(),
                            NewsCache.cached_at >= cutoff,
                        )
                    )
                )
                .scalars()
                .all()
            )
            kap_rows = list(
                (
                    await session.execute(
                        select(KapEvent).where(
                            KapEvent.symbol == symbol.upper(),
                            KapEvent.published_at.is_not(None),
                            KapEvent.published_at >= cutoff,
                            KapEvent.risk_level.in_(("HIGH", "BLOCKING")),
                        )
                    )
                )
                .scalars()
                .all()
            )
        for row in rows:
            text = f"{row.title} {row.content or ''}".casefold()
            for keyword in (
                x.strip()
                for x in settings.news_risk_keywords_csv.split(",")
                if x.strip()
            ):
                if keyword.casefold() in text:
                    return keyword, row.title
        for row in kap_rows:
            return f"KAP {row.risk_level}", row.title
    except Exception:
        return None
    return None


async def apply_news_risk_lock(response: SignalResponse, symbol: str) -> SignalResponse:
    """Block actionable or confirmable BUY proposals; fail open on lookup errors."""
    if response.action != SignalAction.BUY or not (
        response.allow_order or response.requires_confirmation
    ):
        return response
    try:
        risk = await active_news_risk(symbol)
    except Exception:
        return response
    if not risk:
        return response

    keyword, headline = risk
    response.action = SignalAction.WAIT
    response.allow_order = False
    response.requires_confirmation = False
    response.order_type = OrderType.NONE
    response.qty = 0.0
    response.price = None
    response.reason = (
        f"BUY blocked: negative news/KAP risk detected: {keyword} - {headline}"
    )
    return response
