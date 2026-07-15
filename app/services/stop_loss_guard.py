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

from sqlalchemy import select

from app.config import settings
from app.db.session import async_session_factory
from app.models.db import BotPosition, PositionLifecycle
from app.models.signal import OrderType, SignalAction, SignalMode, SignalResponse
from app.services.admin_config import (
    get_scanner_allow_orders,
    get_trading_mode_override,
)
from app.services.daily_trade_count import _start_of_trading_day
from app.services.effective_risk_config import decimal_from_external
from app.services.evaluation.pipeline import EvaluationResult
from app.services.fill_ledger import to_decimal
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    MatriksGatewayClient,
)
from app.services.measurement_repair import enqueue_repair_job
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


async def _resolve_effective_mode(session) -> SignalMode:
    """Mirror evaluate_symbol's mode resolution: admin override, then the
    Phase 2 force-PAPER clamp when order dispatch is globally disabled
    (scannerAllowOrders admin key, falling back to the .env value)."""
    override = await get_trading_mode_override(session)
    mode = (
        override
        if override is not None
        else SignalMode(settings.default_mode.value.upper())
    )
    if not await get_scanner_allow_orders(session) and mode != SignalMode.PAPER:
        mode = SignalMode.PAPER
    return mode


async def _resolve_position_lifecycle(
    symbol: str, *, fallback_qty: float, fallback_avg_price: float | None
) -> PositionLifecycle | None:
    """The stop and remaining quantity now come only from the open position
    lifecycle opened by an actually-filled BUY (Task 4) - never from the most
    recent allow_order=true decision, which may never have filled.

    A position that predates this feature has no lifecycle yet; it is
    backfilled once from BotPosition's current state so protection is not
    silently lost during the cutover (see position_lifecycle_backfill.py).
    """
    async with async_session_factory() as session:
        lifecycle = await get_open_lifecycle(session, symbol)
        if lifecycle is not None:
            return lifecycle
        lifecycle = await ensure_lifecycle_for_legacy_position(
            session,
            symbol=symbol,
            qty=fallback_qty,
            avg_price=fallback_avg_price,
        )
        await session.commit()
        return lifecycle


async def check_stop_loss_positions(
    gateway: MatriksGatewayClient,
) -> list[EvaluationResult]:
    """Return one EXIT_FULL SELL EvaluationResult per breached open position.

    Does not send orders or check kill switch/cutoff - the caller must route
    each result through the normal order-dispatch path for those gates.
    """
    try:
        async with async_session_factory() as session:
            rows = (
                (await session.execute(select(BotPosition).where(BotPosition.qty > 0)))
                .scalars()
                .all()
            )
    except Exception:
        logger.exception("STOP_LOSS_GUARD_POSITION_READ_FAILED")
        return []

    if not rows:
        return []

    async with async_session_factory() as mode_session:
        mode = await _resolve_effective_mode(mode_session)

    triggered: list[EvaluationResult] = []
    for row in rows:
        symbol = row.symbol.strip().upper()
        if row.qty is None or row.qty <= 0:
            continue

        try:
            lifecycle = await _resolve_position_lifecycle(
                symbol, fallback_qty=row.qty, fallback_avg_price=row.avg_price
            )
        except LifecycleIntegrityError as exc:
            # Never guess which of several OPEN rows is authoritative
            # (Task 2.2) - stop updating this symbol and surface it for repair.
            logger.error("STOP_LOSS_GUARD_DUPLICATE_LIFECYCLE symbol=%s", symbol)
            try:
                async with async_session_factory() as repair_session:
                    await enqueue_repair_job(
                        repair_session,
                        repair_type="LIFECYCLE_RECONCILIATION",
                        last_error=repr(exc),
                        symbol=symbol,
                    )
                    await repair_session.commit()
            except Exception:
                logger.exception(
                    "MEASUREMENT_REPAIR_JOB_ENQUEUE_FAILED symbol=%s", symbol
                )
            continue
        except Exception:
            logger.exception("STOP_LOSS_GUARD_LOOKUP_FAILED symbol=%s", symbol)
            continue
        if lifecycle is None or lifecycle.status != "OPEN":
            logger.info(
                "STOP_LOSS_GUARD_NO_OP symbol=%s reason=no_open_lifecycle", symbol
            )
            continue
        qty = lifecycle.current_qty
        if qty is None or qty <= 0:
            continue
        stop_loss = lifecycle.active_stop_loss
        if stop_loss is None or stop_loss <= 0:
            logger.info(
                "STOP_LOSS_GUARD_NO_OP symbol=%s reason=no_active_stop", symbol
            )
            continue
        # Bot-owned qty is integer lots; a lifecycle carries Decimal precision
        # only to support fractional-cost accounting upstream.
        sell_qty = int(qty.to_integral_value())
        if sell_qty <= 0:
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

        last_price = (snapshot.get("payload") or {}).get("lastPrice")
        if last_price is None:
            logger.info("STOP_LOSS_GUARD_NO_OP symbol=%s reason=no_last_price", symbol)
            continue
        last_price_d = to_decimal(last_price)
        if last_price_d is None or last_price_d <= 0 or last_price_d > stop_loss:
            continue

        logger.warning(
            "STOP_LOSS_GUARD_TRIGGERED symbol=%s lastPrice=%s stopLoss=%s qty=%s",
            symbol,
            last_price_d,
            stop_loss,
            sell_qty,
        )
        try:
            async with async_session_factory() as breach_session:
                fresh_lifecycle = await get_open_lifecycle(breach_session, symbol)
                if fresh_lifecycle is not None:
                    await record_stop_breach(
                        breach_session,
                        fresh_lifecycle,
                        source_request_id=None,
                        reason=(
                            f"lastPrice={last_price_d} <= stopLoss={stop_loss}"
                        ),
                    )
                    await breach_session.commit()
        except Exception:
            logger.exception("STOP_LOSS_GUARD_BREACH_AUDIT_FAILED symbol=%s", symbol)

        response = SignalResponse(
            requestId=(
                f"{symbol}-STOPLOSS-{datetime.now(timezone.utc):%Y%m%d%H%M%S%f}"
            ),
            symbol=symbol,
            action=SignalAction.SELL,
            qty=sell_qty,
            orderType=OrderType.LIMIT,
            price=decimal_from_external(float(last_price_d)),
            confidenceScore=100.0,
            riskScore=100.0,
            allowOrder=True,
            requiresConfirmation=False,
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
                mode=mode,
                evaluation_purpose="STOP_LOSS_GUARD",
            )
        )

    return triggered
