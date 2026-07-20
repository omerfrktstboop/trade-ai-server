"""Deterministic, AI-independent stop-loss enforcement.

The scanner's normal order path only re-evaluates a held position through
the AI (on ``scan_interval_minutes``, or ``portfolio_scan_interval_minutes``
for positions that fell off the watchlist). If the AI is unavailable, slow,
or simply returns HOLD/WAIT, a losing position can ride well past its
stop-loss between evaluations. This module closes that gap: it is called
every scanner tick, reads open positions directly from the DB, and compares
a fresh gateway snapshot price against each position's recorded stop —
independent of any AI call.

``check_stop_loss_positions`` only *detects* breaches and builds the exit
decision; it does not send orders itself. The caller (scanner._tick) routes
each returned ``EvaluationResult`` through the existing ``_maybe_send_order``
order-dispatch path, so kill switch, cutoff, preflight, and cooldown gates
apply exactly as they do to every other order - this guard cannot bypass
them, it can only *originate* a SELL for that path to accept or reject.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.db import BotPosition, OrderLog, PositionLifecycle
from app.models.signal import OrderType, SignalAction, SignalResponse
from app.services.admin_config import (
    get_stop_guard_maximum_quote_age_seconds,
)
from app.services.daily_trade_count import _start_of_trading_day
from app.services.effective_risk_config import decimal_from_external
from app.services.evaluation.pipeline import EvaluationResult
from app.services.fill_ledger import to_decimal
from app.services.market_observation import record_market_observation_standalone
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    MatriksGatewayClient,
)
from app.services.measurement_repair import enqueue_repair_job
from app.services.order_ledger import PENDING_STATES
from app.services.position_lifecycle_backfill import ensure_lifecycle_for_legacy_position
from app.services.position_lifecycle_engine import (
    LifecycleIntegrityError,
    get_open_lifecycle,
    record_stop_breach,
)

logger = logging.getLogger(__name__)


class StopLossGuard:
    """Tracks same-day stop-triggered symbols so they aren't immediately re-bought.

    In-memory only, matching the existing scan-timing/order-cooldown state
    on ``SymbolScanner`` (``_last_scan_by_symbol``, ``_last_order_sent_at``):
    it resets on process restart. A restart-persisted ban was judged
    unnecessary scope for this guard - the underlying stop-loss protection
    itself does not depend on this cooldown.
    """

    def __init__(self) -> None:
        self._triggered_on: dict[str, date] = {}

    def is_symbol_cooling_down(self, symbol: str) -> bool:
        triggered_date = self._triggered_on.get(symbol.strip().upper())
        if triggered_date is None:
            return False
        return triggered_date == _start_of_trading_day().date()

    def mark_triggered(self, symbol: str) -> None:
        self._triggered_on[symbol.strip().upper()] = _start_of_trading_day().date()


stop_loss_guard = StopLossGuard()


_QTY_MISMATCH_EPSILON = Decimal("0.0000000001")


async def _enqueue_lifecycle_repair(symbol: str, last_error: str) -> None:
    try:
        async with async_session_factory() as repair_session:
            await enqueue_repair_job(
                repair_session,
                repair_type="LIFECYCLE_RECONCILIATION",
                last_error=last_error,
                symbol=symbol,
            )
            await repair_session.commit()
    except Exception:
        logger.exception("MEASUREMENT_REPAIR_JOB_ENQUEUE_FAILED symbol=%s", symbol)


async def _backfill_missing_lifecycles(bot_positions: list[BotPosition]) -> None:
    """One-time-per-symbol sweep: a BotPosition with qty>0 that has no
    PositionLifecycle history at all (open or closed) is backfilled once, so
    protection is not silently lost for positions that predate this feature
    (Task 6.1). ensure_lifecycle_for_legacy_position is itself a no-op for a
    symbol that already has any lifecycle history, so calling it here for
    every positive BotPosition row is safe. This is the *only* use
    BotPosition still has in this guard, besides the mismatch cross-check
    below - the breach-checking loop reads exclusively from
    PositionLifecycle.
    """
    for row in bot_positions:
        symbol = row.symbol.strip().upper()
        if row.qty is None or row.qty <= 0:
            continue
        try:
            async with async_session_factory() as session:
                await ensure_lifecycle_for_legacy_position(
                    session, symbol=symbol, qty=row.qty, avg_price=row.avg_price
                )
                await session.commit()
        except LifecycleIntegrityError as exc:
            logger.error("STOP_LOSS_GUARD_DUPLICATE_LIFECYCLE symbol=%s", symbol)
            await _enqueue_lifecycle_repair(symbol, repr(exc))
        except Exception:
            logger.exception("STOP_LOSS_GUARD_BACKFILL_FAILED symbol=%s", symbol)


async def check_stop_loss_positions(
    gateway: MatriksGatewayClient,
) -> list[EvaluationResult]:
    """Return one EXIT_FULL SELL EvaluationResult per breached open position.

    Does not send orders or check kill switch/cutoff - the caller must route
    each result through the normal order-dispatch path for those gates.
    """
    try:
        async with async_session_factory() as session:
            bot_positions = (
                (await session.execute(select(BotPosition).where(BotPosition.qty > 0)))
                .scalars()
                .all()
            )
    except Exception:
        logger.exception("STOP_LOSS_GUARD_POSITION_READ_FAILED")
        return []

    await _backfill_missing_lifecycles(bot_positions)
    bot_position_by_symbol = {row.symbol.strip().upper(): row for row in bot_positions}

    # Task 6.1: the guard's primary source is the open lifecycle, not
    # BotPosition - BotPosition is used above only to seed a backfill and
    # below only as a cross-check for data-integrity mismatches.
    try:
        async with async_session_factory() as session:
            lifecycles = (
                (
                    await session.execute(
                        select(PositionLifecycle).where(
                            PositionLifecycle.status == "OPEN",
                            PositionLifecycle.current_qty > 0,
                            PositionLifecycle.active_stop_loss.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
    except Exception:
        logger.exception("STOP_LOSS_GUARD_LIFECYCLE_READ_FAILED")
        return []

    if not lifecycles:
        return []

    async with async_session_factory() as mode_session:
        max_quote_age_seconds = await get_stop_guard_maximum_quote_age_seconds(
            mode_session
        )

    triggered: list[EvaluationResult] = []
    for lifecycle in lifecycles:
        symbol = lifecycle.symbol.strip().upper()
        qty = lifecycle.current_qty
        stop_loss = lifecycle.active_stop_loss
        if qty is None or qty <= 0 or stop_loss is None or stop_loss <= 0:
            continue
        # Bot-owned qty is integer lots; a lifecycle carries Decimal precision
        # only to support fractional-cost accounting upstream.
        sell_qty = int(qty.to_integral_value())
        if sell_qty <= 0:
            continue

        # Task 6.3: lifecycle vs BotPosition mismatch - never auto-sell the
        # larger figure or silently pick one; stop and surface for repair.
        bot_position = bot_position_by_symbol.get(symbol)
        if bot_position is not None and bot_position.qty is not None:
            bot_qty_d = to_decimal(bot_position.qty)
            if bot_qty_d is not None and abs(bot_qty_d - qty) > _QTY_MISMATCH_EPSILON:
                logger.error(
                    "STOP_GUARD_POSITION_MISMATCH symbol=%s lifecycleQty=%s "
                    "botPositionQty=%s",
                    symbol,
                    qty,
                    bot_qty_d,
                )
                await _enqueue_lifecycle_repair(
                    symbol,
                    f"STOP_GUARD_POSITION_MISMATCH lifecycleQty={qty} "
                    f"botPositionQty={bot_qty_d}",
                )
                continue

        # Task 6.4: never re-trigger while a SELL for this symbol is already
        # in flight - the pending order already represents this breach.
        try:
            async with async_session_factory() as session:
                pending_sell = (
                    await session.execute(
                        select(OrderLog.id)
                        .where(
                            OrderLog.symbol == symbol,
                            OrderLog.action == "SELL",
                            OrderLog.status.in_(PENDING_STATES),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
        except Exception:
            logger.exception(
                "STOP_LOSS_GUARD_PENDING_ORDER_CHECK_FAILED symbol=%s", symbol
            )
            pending_sell = None
        if pending_sell is not None:
            logger.info(
                "STOP_LOSS_GUARD_NO_OP symbol=%s reason=sell_already_pending", symbol
            )
            continue

        try:
            snapshot = await gateway.get_snapshot(symbol)
        except (GatewayUnavailable, GatewayError) as exc:
            logger.warning(
                "STOP_LOSS_GUARD_SNAPSHOT_UNAVAILABLE symbol=%s error=%s", symbol, exc
            )
            continue
        except Exception:
            logger.exception("STOP_LOSS_GUARD_SNAPSHOT_FAILED symbol=%s", symbol)
            continue

        guard_payload = snapshot.get("payload") or {}
        await record_market_observation_standalone(symbol, guard_payload)

        # Task 6.2 / Fix 2: a stop may only be triggered by a fresh, reliable
        # price. A missing quoteAgeSeconds is treated as untrustworthy, not as
        # "age 0" - without a known age we cannot assert the quote is fresh.
        if not guard_payload.get("quoteReliable"):
            logger.info(
                "STOP_LOSS_GUARD_NO_OP symbol=%s reason=STOP_GUARD_QUOTE_UNRELIABLE",
                symbol,
            )
            continue
        quote_age_seconds = to_decimal(guard_payload.get("quoteAgeSeconds"))
        if quote_age_seconds is None or quote_age_seconds < 0:
            logger.info(
                "STOP_LOSS_GUARD_NO_OP symbol=%s reason=STOP_GUARD_QUOTE_STALE "
                "quoteAgeSeconds=%s detail=missing_or_negative",
                symbol,
                quote_age_seconds,
            )
            continue
        if quote_age_seconds > max_quote_age_seconds:
            logger.info(
                "STOP_LOSS_GUARD_NO_OP symbol=%s reason=STOP_GUARD_QUOTE_STALE "
                "quoteAgeSeconds=%s maxAllowed=%s",
                symbol,
                quote_age_seconds,
                max_quote_age_seconds,
            )
            continue
        if not guard_payload.get("priceSource"):
            logger.info(
                "STOP_LOSS_GUARD_NO_OP symbol=%s reason=STOP_GUARD_PRICE_INVALID "
                "detail=missing_price_source",
                symbol,
            )
            continue
        last_price_d = to_decimal(guard_payload.get("lastPrice"))
        if last_price_d is None or last_price_d <= 0:
            logger.info(
                "STOP_LOSS_GUARD_NO_OP symbol=%s reason=STOP_GUARD_PRICE_INVALID "
                "detail=non_finite_or_nonpositive",
                symbol,
            )
            continue
        if last_price_d > stop_loss:
            continue

        logger.warning(
            "STOP_LOSS_GUARD_TRIGGERED symbol=%s lastPrice=%s stopLoss=%s qty=%s",
            symbol,
            last_price_d,
            stop_loss,
            sell_qty,
        )
        # Request id, breach olayından ÖNCE üretilir ve olaya bağlanır:
        # audit-yoksa-emir-yok kapısı (v2 ilke #6) bu STOP_BREACHED kaydını
        # dispatch yetkisi olarak arar — commit edilmeden emir gönderilemez.
        request_id = (
            f"{symbol}-STOPLOSS-{datetime.now(timezone.utc):%Y%m%d%H%M%S%f}"
        )
        try:
            async with async_session_factory() as breach_session:
                fresh_lifecycle = await get_open_lifecycle(breach_session, symbol)
                if fresh_lifecycle is not None:
                    await record_stop_breach(
                        breach_session,
                        fresh_lifecycle,
                        source_request_id=request_id,
                        reason=(
                            f"lastPrice={last_price_d} <= stopLoss={stop_loss}"
                        ),
                    )
                    await breach_session.commit()
        except Exception:
            logger.exception("STOP_LOSS_GUARD_BREACH_AUDIT_FAILED symbol=%s", symbol)

        response = SignalResponse(
            requestId=request_id,
            symbol=symbol,
            action=SignalAction.SELL,
            qty=sell_qty,
            orderType=OrderType.LIMIT,
            price=decimal_from_external(float(last_price_d)),
            confidenceScore=100.0,
            riskScore=100.0,
            allowOrder=True,
            reason=(
                f"Stop-loss guard: lastPrice={last_price_d} <= stopLoss={stop_loss}, "
                "deterministic exit independent of AI"
            ),
            entryRange=None,
            stopLoss=decimal_from_external(float(stop_loss)),
            targetPrice=None,
        )
        triggered.append(
            EvaluationResult(
                response=response,
                dispatch_eligible=True,
                evaluation_purpose="STOP_LOSS_GUARD",
            )
        )

    return triggered
