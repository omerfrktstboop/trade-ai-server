"""Idempotent outcome labeler (Task 3.3 / 3.4): fills in forward returns,
MFE/MAE, and target-vs-stop-first for every DecisionOutcome using only real,
currently-observable gateway prices.

Design constraint: the gateway's /bars surface carries no per-bar timestamp
and does not accept a timeframe override (confirmed in TradeAiGateway.cs
HandleBarsAsync - the current usage in discovery_agent.py only ever consumes
DAILY bars). There is therefore no reliable way to retroactively look up
"the price 5 minutes after decision X" from stored bars without guessing bar
boundaries, which the task's own rule forbids. Instead this labeler polls a
*live* reliable snapshot the moment each horizon elapses and records the
actually observed price - never a fabricated or interpolated one. A horizon
whose time has come but for which no reliable snapshot was captured before
the next run is left exactly as it was (still None, not zero) until either a
later run succeeds or the window is old enough to be marked unavailable.

Callable as:
    python -m app.services.outcome_labeler
or as ``label_pending_outcomes()`` from a background task/admin endpoint -
either way it never touches the scanner tick or the order-dispatch path.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.db import DecisionOutcome
from app.services.admin_config import build_runtime_risk_config
from app.services.fill_ledger import to_decimal
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    MatriksGatewayClient,
    gateway_client,
)

logger = logging.getLogger(__name__)

HORIZONS_MINUTES: tuple[tuple[str, int], ...] = (
    ("future_return_5m", 5),
    ("future_return_15m", 15),
    ("future_return_30m", 30),
    ("future_return_60m", 60),
)
ALL_RETURN_FIELDS = tuple(name for name, _ in HORIZONS_MINUTES) + ("future_return_eod",)

# A due horizon polled this long after it elapsed without ever seeing a
# reliable price is treated as unrecoverable rather than left pending
# forever (e.g. the process was down, or the symbol lost gateway coverage).
_STALE_HORIZON_GRACE = timedelta(hours=6)


@dataclass
class LabelerStats:
    processed: int = 0
    updated_fields: int = 0
    completed: int = 0
    marked_unavailable: int = 0


def _forward_return_pct(decision_price: Decimal, observed_price: Decimal) -> Decimal:
    return ((observed_price - decision_price) / decision_price) * Decimal(100)


async def _fetch_reliable_price(
    gateway: MatriksGatewayClient, symbol: str
) -> Decimal | None:
    """None on any unavailability/unreliability - never a guessed price."""
    try:
        snapshot = await gateway.get_snapshot(symbol)
    except (GatewayUnavailable, GatewayError):
        return None
    except Exception:
        logger.exception("OUTCOME_LABELER_SNAPSHOT_FAILED symbol=%s", symbol)
        return None
    payload = snapshot.get("payload") or {}
    if not payload.get("quoteReliable"):
        return None
    price = to_decimal(payload.get("lastPrice"))
    if price is None or price <= 0:
        return None
    return price


def _update_mfe_mae(
    outcome: DecisionOutcome, decision_price: Decimal, observed_price: Decimal
) -> None:
    """Running extrema over every reliable price observed since decision_at.
    Re-running the labeler only ever extends these - naturally idempotent."""
    candidate = _forward_return_pct(decision_price, observed_price)
    if outcome.mfe_pct is None or candidate > outcome.mfe_pct:
        outcome.mfe_pct = candidate
    if outcome.mae_pct is None or candidate < outcome.mae_pct:
        outcome.mae_pct = candidate


def _check_target_stop(
    outcome: DecisionOutcome, observed_price: Decimal, observed_at: datetime
) -> None:
    """Whichever of target/stop is observed true FIRST (across labeler runs)
    determines target_hit_before_stop, set once and never changed again. If
    both are true on the very first observation, the polling granularity
    cannot tell which happened first -> AMBIGUOUS, per Task 3.3.

    Only BUY decisions have a directional target/stop; other actions simply
    never populate these fields (their raw forward return is still recorded
    via the horizon fields above).
    """
    if outcome.decision_action != "BUY":
        return
    if outcome.outcome_status == "AMBIGUOUS":
        return

    target_hit = outcome.target_price is not None and observed_price >= outcome.target_price
    stop_hit = outcome.stop_loss is not None and observed_price <= outcome.stop_loss
    already_resolved = outcome.target_hit_at is not None or outcome.stop_hit_at is not None

    if not already_resolved:
        if target_hit and stop_hit:
            outcome.target_hit_at = observed_at
            outcome.stop_hit_at = observed_at
            outcome.target_hit_before_stop = None
            outcome.outcome_status = "AMBIGUOUS"
            return
        if target_hit:
            outcome.target_hit_at = observed_at
            outcome.target_hit_before_stop = True
            return
        if stop_hit:
            outcome.stop_hit_at = observed_at
            outcome.target_hit_before_stop = False
            return
        return

    # The verdict is already locked in; only backfill the other side's
    # observed timestamp for record-keeping, never flip the verdict.
    if target_hit and outcome.target_hit_at is None:
        outcome.target_hit_at = observed_at
    if stop_hit and outcome.stop_hit_at is None:
        outcome.stop_hit_at = observed_at


async def label_pending_outcomes(
    gateway: MatriksGatewayClient | None = None,
) -> LabelerStats:
    gateway = gateway or gateway_client
    stats = LabelerStats()
    now = datetime.now(timezone.utc)

    async with async_session_factory() as session:
        try:
            runtime_config = await build_runtime_risk_config(session)
            session_open = runtime_config.can_trade_now(now)
        except Exception:
            logger.exception("OUTCOME_LABELER_RUNTIME_CONFIG_FAILED")
            session_open = True  # fail closed on EOD labeling, not on the run

        rows = (
            (
                await session.execute(
                    select(DecisionOutcome)
                    .where(DecisionOutcome.outcome_status.in_(("PENDING", "PARTIAL")))
                    .order_by(DecisionOutcome.decision_at.asc())
                )
            )
            .scalars()
            .all()
        )

        for outcome in rows:
            stats.processed += 1
            if outcome.decision_price is None:
                outcome.outcome_status = "UNAVAILABLE"
                outcome.unavailable_reason = "decision_price missing at creation time"
                stats.marked_unavailable += 1
                continue

            decision_at = outcome.decision_at
            if decision_at.tzinfo is None:
                decision_at = decision_at.replace(tzinfo=timezone.utc)

            due_fields = [
                field
                for field, minutes in HORIZONS_MINUTES
                if getattr(outcome, field) is None
                and now >= decision_at + timedelta(minutes=minutes)
            ]
            needs_eod = outcome.future_return_eod is None and not session_open
            needs_target_stop_poll = (
                outcome.decision_action == "BUY"
                and outcome.outcome_status != "AMBIGUOUS"
                and (outcome.stop_loss is not None or outcome.target_price is not None)
                and (outcome.target_hit_at is None or outcome.stop_hit_at is None)
            )

            if not (due_fields or needs_eod or needs_target_stop_poll):
                continue

            price = await _fetch_reliable_price(gateway, outcome.symbol)
            if price is None:
                if due_fields:
                    oldest_due_age = now - (
                        decision_at + timedelta(minutes=min(m for _, m in HORIZONS_MINUTES))
                    )
                    if oldest_due_age > _STALE_HORIZON_GRACE:
                        outcome.unavailable_reason = (
                            "no reliable gateway price observed for a due "
                            f"horizon as of {now.isoformat()}"
                        )
                        if outcome.outcome_status == "PENDING":
                            outcome.outcome_status = "PARTIAL"
                continue

            for field in due_fields:
                setattr(outcome, field, _forward_return_pct(outcome.decision_price, price))
                stats.updated_fields += 1
            if needs_eod:
                outcome.future_return_eod = _forward_return_pct(outcome.decision_price, price)
                stats.updated_fields += 1
            _update_mfe_mae(outcome, outcome.decision_price, price)
            if needs_target_stop_poll:
                _check_target_stop(outcome, price, now)

            if outcome.outcome_status != "AMBIGUOUS":
                filled = [getattr(outcome, f) is not None for f in ALL_RETURN_FIELDS]
                if all(filled):
                    if outcome.outcome_status != "COMPLETE":
                        stats.completed += 1
                    outcome.outcome_status = "COMPLETE"
                elif any(filled):
                    outcome.outcome_status = "PARTIAL"

        await session.commit()

    return stats


async def run_once() -> LabelerStats:
    return await label_pending_outcomes()


def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    stats = asyncio.run(run_once())
    logger.info(
        "OUTCOME_LABELER_RUN_COMPLETE processed=%s updatedFields=%s completed=%s "
        "markedUnavailable=%s",
        stats.processed,
        stats.updated_fields,
        stats.completed,
        stats.marked_unavailable,
    )


if __name__ == "__main__":
    _main()
