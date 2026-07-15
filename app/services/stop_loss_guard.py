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
from app.models.db import BotPosition, RiskDecision
from app.models.signal import OrderType, SignalAction, SignalMode, SignalResponse
from app.services.admin_config import get_trading_mode_override
from app.services.daily_trade_count import _start_of_trading_day
from app.services.effective_risk_config import decimal_from_external
from app.services.evaluation.pipeline import EvaluationResult
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    MatriksGatewayClient,
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
    Phase 2 force-PAPER clamp when order dispatch is globally disabled."""
    override = await get_trading_mode_override(session)
    mode = (
        override
        if override is not None
        else SignalMode(settings.default_mode.value.upper())
    )
    if not settings.scanner_allow_orders and mode != SignalMode.PAPER:
        mode = SignalMode.PAPER
    return mode


async def _resolve_stop_loss(symbol: str) -> float | None:
    """The stop recorded at the position's opening decision.

    BotPosition carries no live stop_loss column, so this looks up the most
    recent allowed BUY decision for the symbol instead.
    """
    async with async_session_factory() as session:
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
        return (await session.execute(stmt)).scalar_one_or_none()


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
        qty = int(row.qty)
        if qty <= 0:
            continue

        try:
            stop_loss = await _resolve_stop_loss(symbol)
        except Exception:
            logger.exception("STOP_LOSS_GUARD_LOOKUP_FAILED symbol=%s", symbol)
            continue
        if stop_loss is None:
            logger.info(
                "STOP_LOSS_GUARD_NO_OP symbol=%s reason=no_recorded_stop", symbol
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

        last_price = (snapshot.get("payload") or {}).get("lastPrice")
        if last_price is None:
            logger.info("STOP_LOSS_GUARD_NO_OP symbol=%s reason=no_last_price", symbol)
            continue
        last_price = float(last_price)
        if last_price <= 0 or last_price > stop_loss:
            continue

        logger.warning(
            "STOP_LOSS_GUARD_TRIGGERED symbol=%s lastPrice=%s stopLoss=%s qty=%s",
            symbol,
            last_price,
            stop_loss,
            qty,
        )
        response = SignalResponse(
            requestId=(
                f"{symbol}-STOPLOSS-{datetime.now(timezone.utc):%Y%m%d%H%M%S%f}"
            ),
            symbol=symbol,
            action=SignalAction.SELL,
            qty=qty,
            orderType=OrderType.LIMIT,
            price=decimal_from_external(last_price),
            confidenceScore=100.0,
            riskScore=100.0,
            allowOrder=True,
            requiresConfirmation=False,
            reason=(
                f"Stop-loss guard: lastPrice={last_price} <= stopLoss={stop_loss}, "
                "deterministic exit independent of AI"
            ),
            entryRange=None,
            stopLoss=decimal_from_external(stop_loss),
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
