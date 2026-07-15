"""One-time reconciliation: a BotPosition with qty > 0 that predates this
feature has no PositionLifecycle yet, because no fill was ever recorded
through the new fill ledger for it. Without this, the stop-loss guard would
silently lose protection for every already-open position the instant it
switches from the old "last BUY decision" heuristic to lifecycle-only
sourcing (Task 4). This module seeds exactly one lifecycle per such symbol
from the best real data actually available - BotPosition's current qty/avg
cost, and the same legacy "most recent allowed BUY decision" heuristic for
an initial stop - and marks the parts that cannot be reliably known
(historical buy cost, entry timing) rather than fabricating them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import RiskDecision
from app.models.signal import SignalAction
from app.services.fill_ledger import to_decimal
from app.services.position_lifecycle_engine import get_open_lifecycle
from app.models.db import PositionLifecycle
from app.services.strategy_provenance import (
    DECISION_CONTEXT_SCHEMA_VERSION,
    PROMPT_VERSION,
    STRATEGY_VERSION,
    resolve_ai_provider_model,
)

logger = logging.getLogger(__name__)

BACKFILL_UNAVAILABLE = "BACKFILL_UNAVAILABLE"


async def _legacy_stop_heuristic(session: AsyncSession, symbol: str) -> Decimal | None:
    """The pre-Task-4 stop source, used only to seed a backfilled lifecycle
    so already-open positions do not lose protection during the cutover."""
    stmt = (
        select(RiskDecision.stop_loss)
        .where(
            RiskDecision.symbol == symbol,
            RiskDecision.action == SignalAction.BUY.value,
            RiskDecision.allow_order.is_(True),
            RiskDecision.stop_loss.is_not(None),
        )
        .order_by(RiskDecision.created_at.desc())
        .limit(1)
    )
    value = (await session.execute(stmt)).scalar_one_or_none()
    stop = to_decimal(value)
    return stop if stop is not None and stop > 0 else None


async def ensure_lifecycle_for_legacy_position(
    session: AsyncSession,
    *,
    symbol: str,
    qty: float | Decimal,
    avg_price: float | Decimal | None,
) -> PositionLifecycle | None:
    """Return the open lifecycle for ``symbol``, creating a backfilled one
    from BotPosition only if the symbol has *no* lifecycle history at all -
    i.e. it truly predates this feature. Idempotent: re-checks under lock.

    Guards against a narrow but real race: BotPosition is refreshed from the
    gateway on its own sync interval, so right after a real position fully
    closes there is a window where BotPosition.qty is still stale-positive.
    If a lifecycle (open OR closed) already exists for the symbol, that
    proves the fill ledger already covers it, so a stale positive qty must
    never re-create/backfill a lifecycle from guessed data - the guard
    simply waits for BotPosition to catch up instead.
    """
    symbol = symbol.strip().upper()
    existing = await get_open_lifecycle(session, symbol, for_update=True)
    if existing is not None:
        return existing
    has_any_history = (
        await session.execute(
            select(PositionLifecycle.id).where(PositionLifecycle.symbol == symbol).limit(1)
        )
    ).scalar_one_or_none()
    if has_any_history is not None:
        return None

    qty_d = to_decimal(qty)
    if qty_d is None or qty_d <= 0:
        return None
    avg_price_d = to_decimal(avg_price)

    stop = await _legacy_stop_heuristic(session, symbol)
    ai_provider, ai_model = resolve_ai_provider_model()
    lifecycle = PositionLifecycle(
        symbol=symbol,
        status="OPEN",
        opened_at=datetime.now(timezone.utc),
        entry_request_id=None,
        entry_order_id=None,
        current_qty=qty_d,
        average_entry_price=avg_price_d,
        gross_buy_value_tl=(qty_d * avg_price_d) if avg_price_d is not None else Decimal("0"),
        total_buy_cost_tl=Decimal("0"),
        initial_stop_loss=stop,
        active_stop_loss=stop,
        initial_target_price=None,
        active_target_price=None,
        strategy_version=STRATEGY_VERSION,
        prompt_version=PROMPT_VERSION,
        decision_context_schema_version=DECISION_CONTEXT_SCHEMA_VERSION,
        config_hash=BACKFILL_UNAVAILABLE,
        profile_code=BACKFILL_UNAVAILABLE,
        ai_provider=ai_provider,
        ai_model=ai_model,
        decision_source=None,
        data_quality=BACKFILL_UNAVAILABLE,
        is_backfilled=True,
        backfill_reason="pre_existing_position_without_recorded_fills",
        pnl_verified=False,
        measurement_source="LEGACY_POSITION_BACKFILL",
    )
    session.add(lifecycle)
    await session.flush()
    logger.warning(
        "POSITION_LIFECYCLE_BACKFILLED symbol=%s qty=%s stop=%s "
        "reason=pre_existing_position_without_recorded_fills",
        symbol,
        qty_d,
        stop,
    )
    return lifecycle
