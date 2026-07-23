"""Fill-sonrası durable re-entry cooldown (Plan Faz 2.4).

Cooldown, BUY emrinin oluşturulma zamanından değil, pozisyonu tamamen kapatan
final SELL fill'inden (lifecycle ``closed_at``) başlar ve çıkış nedenine göre
değişir (plan bölüm 7): kâr-al sonrası kısa, zaman çıkışı sonrası orta, stop
sonrası uzun. Kayıt DB'de (position_lifecycles + exit_intents) yaşadığı için
cooldown restart'ı da hayatta bırakır.

``reentry_cooldown_block`` bir engel gerekçesi döndürür (ya da None). Çağıran
(deterministik entry pipeline) bunu yalnızca flag açıkken uygular ve engel
varsa BUY'u WAIT'e indirir.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import ExitIntent, PositionLifecycle
from app.services.exit_policy import ExitPolicy

logger = logging.getLogger(__name__)

# Çıkış nedeni -> hangi cooldown süresi (plan bölüm 7).
_TAKE_PROFIT_REASONS = {"HARD_TARGET", "TRAILING"}
_TIME_EXIT_REASONS = {"STAGNATION", "MAX_HOLD"}
_STOP_REASONS = {"STOP", "BREAKEVEN"}


def _cooldown_minutes(reason: str | None, policy: ExitPolicy) -> float:
    if reason in _TAKE_PROFIT_REASONS:
        return policy.reentry_cooldown_take_profit_minutes
    if reason in _TIME_EXIT_REASONS:
        return policy.reentry_cooldown_time_exit_minutes
    if reason in _STOP_REASONS:
        return policy.reentry_cooldown_stop_minutes
    # Neden bilinmiyorsa en muhafazakâr (en uzun) bekleme.
    return policy.reentry_cooldown_stop_minutes


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def reentry_cooldown_block(
    session: AsyncSession,
    symbol: str,
    policy: ExitPolicy,
    *,
    now: datetime | None = None,
) -> str | None:
    """Sembol re-entry cooldown içindeyse engel gerekçesi, değilse None.

    En son tamamen kapanmış lifecycle'ın ``closed_at``'inden başlar; çıkış
    nedeni o lifecycle'a bağlı en güncel ExitIntent'ten okunur (yoksa en
    muhafazakâr stop cooldown'u uygulanır).
    """
    now = now or datetime.now(timezone.utc)
    symbol = symbol.upper()

    lifecycle = (
        await session.execute(
            select(PositionLifecycle)
            .where(
                PositionLifecycle.symbol == symbol,
                PositionLifecycle.status == "CLOSED",
                PositionLifecycle.closed_at.is_not(None),
            )
            .order_by(PositionLifecycle.closed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if lifecycle is None or lifecycle.closed_at is None:
        return None

    closed_at = _aware(lifecycle.closed_at)

    intent = (
        await session.execute(
            select(ExitIntent)
            .where(ExitIntent.position_lifecycle_id == lifecycle.id)
            .order_by(ExitIntent.trigger_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    reason = intent.exit_reason if intent is not None else None

    minutes = _cooldown_minutes(reason, policy)
    unblock_at = closed_at + timedelta(minutes=minutes)
    remaining = (unblock_at - now).total_seconds()
    if remaining <= 0:
        return None

    return (
        f"Re-entry cooldown after {reason or 'exit'}: "
        f"{int(remaining)}s remaining (closed {closed_at.isoformat()}, "
        f"{minutes:g}min window)"
    )
