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
from app.models.db import OrderLog, PositionLifecycle
from app.models.signal import OrderType, SignalAction, SignalResponse
from app.services.admin_config import (
    get_stop_guard_maximum_quote_age_seconds,
)
from app.services.bot_ownership import load_bot_ownership
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
from app.services.order_ledger import PENDING_STATES
from app.services.position_lifecycle_engine import (
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


async def check_stop_loss_positions(
    gateway: MatriksGatewayClient,
) -> list[EvaluationResult]:
    """Return one EXIT_FULL SELL EvaluationResult per breached open position.

    Does not send orders or check kill switch/cutoff - the caller must route
    each result through the normal order-dispatch path for those gates.
    """
    try:
        health = await gateway.health()
        positions = await gateway.get_positions()
        account_ref = str(health.get("accountRef") or "").strip()
        session_ref = str(health.get("accountSessionRef") or "").strip()
        if (
            len(account_ref) != 64
            or len(session_ref) != 64
            or str(positions.get("accountRef") or "").strip() != account_ref
            or str(positions.get("accountSessionRef") or "").strip() != session_ref
            or positions.get("positionsLoaded") is not True
            or positions.get("snapshotCompleteFlag") is not True
            or str(positions.get("confidence") or "").upper()
            not in {"HIGH", "MEDIUM"}
        ):
            return []
        gateway_positions = {
            str(row.get("symbol") or "").strip().upper(): row
            for row in positions.get("positions") or []
            if isinstance(row, dict) and str(row.get("symbol") or "").strip()
        }
        async with async_session_factory() as session:
            ownership = await load_bot_ownership(session, account_ref)
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
        gateway_position = gateway_positions.get(symbol)
        ledger_qty = ownership.quantities.get(symbol, Decimal("0"))
        gateway_bot_qty = to_decimal(
            (gateway_position or {}).get("botOwnedQty")
            if gateway_position is not None
            else None
        )
        if gateway_bot_qty is None and gateway_position is not None:
            gateway_bot_qty = to_decimal(gateway_position.get("botQty"))
        async with async_session_factory() as session:
            entry_order = (
                await session.execute(
                    select(OrderLog.id)
                    .where(
                        OrderLog.request_id == lifecycle.entry_request_id,
                        OrderLog.account_ref == account_ref,
                        OrderLog.request_fingerprint.is_not(None),
                        OrderLog.action == "BUY",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
        if (
            entry_order is None
            or lifecycle.is_backfilled
            or lifecycle.data_quality not in {"VERIFIED", "RECONCILED"}
            or ledger_qty != qty
            or gateway_bot_qty != qty
        ):
            logger.error(
                "STOP_GUARD_ACCOUNT_OWNERSHIP_MISMATCH symbol=%s lifecycleQty=%s "
                "ledgerQty=%s gatewayBotQty=%s",
                symbol,
                qty,
                ledger_qty,
                gateway_bot_qty,
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
                            OrderLog.account_ref == account_ref,
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
                fresh_lifecycle = await breach_session.get(
                    PositionLifecycle, lifecycle.id, with_for_update=True
                )
                if (
                    fresh_lifecycle is not None
                    and fresh_lifecycle.status == "OPEN"
                    and fresh_lifecycle.current_qty == qty
                ):
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
