"""Outcome labeler (Task 4): fills in real, timestamp-anchored forward
returns, MFE/MAE, and target-vs-stop-first for every DecisionOutcome, using
only the MarketObservation rows collected by Task 3 - never a single
"whatever the price is right now" snapshot applied to every due field.

For each numeric horizon (5/15/30/60 minutes) the labeler computes
target_time = decision_at + horizon and selects the first reliable
observation at/after target_time within outcomeMaximumObservationDelaySeconds
(admin config, default 120s). A horizon whose window has no reliable
observation stays exactly None - with a reason code - until either a later
run finds one or the window is old enough to be treated as a permanent data
gap. EOD reuses the same window-selection logic against the marketSessionCloseTime
instant (Fix 6), which is separate from the order cutoff (disableTradingAfter).
MFE/MAE and target/stop-first are computed from trustworthy post-decision
price points within a bounded window (Fix 4/5): a bar that verifiably started
at/after the decision contributes full OHLC, the decision-crossing/unknown
bar contributes only its reliable tick, and nothing past EOD is counted, so
the next session's prices can never bleed into a prior decision's excursions.

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
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.db import DecisionOutcome, MarketObservation
from app.services.admin_config import (
    build_runtime_risk_config,
    get_market_session_close_time,
    get_outcome_maximum_observation_delay_seconds,
)

logger = logging.getLogger(__name__)

HORIZONS_MINUTES: tuple[tuple[str, int], ...] = (
    ("future_return_5m", 5),
    ("future_return_15m", 15),
    ("future_return_30m", 30),
    ("future_return_60m", 60),
)
ALL_RETURN_FIELDS = tuple(name for name, _ in HORIZONS_MINUTES) + ("future_return_eod",)
_MAX_HORIZON_MINUTES = max(minutes for _, minutes in HORIZONS_MINUTES)

# Past this age, a horizon that never found a qualifying observation is
# treated as a permanent gap (DATA_GAP) rather than retried forever.
_HORIZON_GRACE = timedelta(hours=6)


@dataclass
class LabelerStats:
    processed: int = 0
    updated_fields: int = 0
    completed: int = 0
    data_gap: int = 0
    ambiguous: int = 0


def _forward_return_pct(decision_price: Decimal, observed_price: Decimal) -> Decimal:
    return ((observed_price - decision_price) / decision_price) * Decimal(100)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _select_observation_in_window(
    session, symbol: str, target_time: datetime, max_delay_seconds: int
) -> tuple[MarketObservation | None, str | None]:
    """First reliable observation at/after target_time within the delay
    window, or (None, reason_code) explaining why not (Task 4.1)."""
    window_end = target_time + timedelta(seconds=max_delay_seconds)

    reliable_stmt = (
        select(MarketObservation)
        .where(
            MarketObservation.symbol == symbol,
            MarketObservation.observed_at >= target_time,
            MarketObservation.observed_at <= window_end,
            MarketObservation.quote_reliable.is_(True),
        )
        .order_by(MarketObservation.observed_at.asc())
        .limit(1)
    )
    obs = (await session.execute(reliable_stmt)).scalars().first()
    if obs is not None:
        return obs, None

    any_in_window = (
        await session.execute(
            select(MarketObservation.id)
            .where(
                MarketObservation.symbol == symbol,
                MarketObservation.observed_at >= target_time,
                MarketObservation.observed_at <= window_end,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if any_in_window is not None:
        return None, "QUOTE_UNRELIABLE"

    any_after_window = (
        await session.execute(
            select(MarketObservation.id)
            .where(
                MarketObservation.symbol == symbol,
                MarketObservation.observed_at > window_end,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if any_after_window is not None:
        return None, "OBSERVATION_TOO_LATE"

    return None, "NO_OBSERVATION_IN_WINDOW"


def _eod_target_time(
    decision_at: datetime, timezone_name: str, session_close: str
) -> datetime | None:
    """The session-close instant on the decision's own calendar day (Fix 6),
    derived from marketSessionCloseTime - deliberately NOT disableTradingAfter
    (the order cutoff), which can be much earlier than the actual close."""
    try:
        tz = ZoneInfo(timezone_name)
        hour, minute = map(int, session_close.split(":"))
    except (ValueError, KeyError):
        return None
    local_decision = decision_at.astimezone(tz)
    local_close = local_decision.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return local_close.astimezone(timezone.utc)


def _measurement_end(
    decision_at: datetime, eod_target_time: datetime | None
) -> datetime:
    """Upper bound for MFE/MAE and target/stop scanning (Fix 5): never past
    EOD, so the next session's prices can never bleed into a prior decision's
    excursions. When EOD is unknown, the last numeric horizon is the cap."""
    last_horizon_end = decision_at + timedelta(minutes=_MAX_HORIZON_MINUTES)
    if eod_target_time is not None:
        return max(last_horizon_end, eod_target_time)
    return last_horizon_end


def _extract_extremes(
    row: MarketObservation, decision_at: datetime
) -> tuple[Decimal, Decimal] | None:
    """Return (high_candidate, low_candidate) for one observation, or None if
    it cannot contribute a trustworthy post-decision price (Fix 4).

    A bar that verifiably *started at/after* the decision is used as full
    OHLC. The decision-crossing bar (started before decision) and any bar
    with an unknown start are NOT used as OHLC - their high/low may reflect
    pre-decision movement - so only their reliable last-price tick is used,
    contributing a single point that is both the high and low candidate.
    """
    bar_start = _aware(row.bar_start_at) if row.bar_start_at is not None else None
    if (
        bar_start is not None
        and bar_start >= decision_at
        and row.ohlc_reliable
        and row.high is not None
        and row.low is not None
    ):
        return row.high, row.low
    if row.quote_reliable and row.last_price is not None:
        return row.last_price, row.last_price
    return None


async def _resolve_due_horizons(
    session,
    outcome: DecisionOutcome,
    *,
    now: datetime,
    decision_at: datetime,
    max_delay_seconds: int,
    eod_target_time: datetime | None,
) -> tuple[list[str], dict[str, str]]:
    """Return (fields_updated_this_pass, reason_by_unresolved_field). Never
    writes the same observation's price into more than one horizon (Task
    4.2) - each field resolves against its own target_time independently."""
    updated: list[str] = []
    reasons: dict[str, str] = {}

    for field, minutes in HORIZONS_MINUTES:
        if getattr(outcome, field) is not None:
            continue
        target_time = decision_at + timedelta(minutes=minutes)
        if now < target_time:
            continue
        obs, reason = await _select_observation_in_window(
            session, outcome.symbol, target_time, max_delay_seconds
        )
        if obs is not None and obs.last_price is not None:
            setattr(
                outcome, field, _forward_return_pct(outcome.decision_price, obs.last_price)
            )
            updated.append(field)
        elif reason is not None:
            reasons[field] = reason

    if outcome.future_return_eod is None and eod_target_time is not None and now >= eod_target_time:
        obs, reason = await _select_observation_in_window(
            session, outcome.symbol, eod_target_time, max_delay_seconds
        )
        if obs is not None and obs.last_price is not None:
            outcome.future_return_eod = _forward_return_pct(
                outcome.decision_price, obs.last_price
            )
            updated.append("future_return_eod")
        elif reason is not None:
            reasons["future_return_eod"] = reason

    return updated, reasons


async def _observations_for_excursion(
    session, outcome: DecisionOutcome, decision_at: datetime, measurement_end: datetime
) -> list[MarketObservation]:
    """Observations strictly inside (decision_at, measurement_end], ordered.
    The upper bound is the Fix 5 measurement end; the per-row post-decision
    check happens in _extract_extremes."""
    stmt = (
        select(MarketObservation)
        .where(
            MarketObservation.symbol == outcome.symbol,
            MarketObservation.observed_at >= decision_at,
            MarketObservation.observed_at <= measurement_end,
        )
        .order_by(MarketObservation.observed_at.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def _update_mfe_mae(
    session, outcome: DecisionOutcome, decision_at: datetime, measurement_end: datetime
) -> None:
    """Recomputed each pass from every trustworthy post-decision price point
    within the bounded window (Fix 4/5) - full OHLC for bars that started
    after the decision, reliable tick otherwise - never a single labeler-
    run-time lastPrice, and never past EOD."""
    rows = await _observations_for_excursion(
        session, outcome, decision_at, measurement_end
    )
    highs: list[Decimal] = []
    lows: list[Decimal] = []
    for row in rows:
        extremes = _extract_extremes(row, decision_at)
        if extremes is None:
            continue
        highs.append(extremes[0])
        lows.append(extremes[1])
    if highs:
        outcome.mfe_pct = _forward_return_pct(outcome.decision_price, max(highs))
    if lows:
        outcome.mae_pct = _forward_return_pct(outcome.decision_price, min(lows))


async def _update_target_stop_order(
    session, outcome: DecisionOutcome, decision_at: datetime, measurement_end: datetime
) -> None:
    """Scans trustworthy post-decision price points in chronological order
    for the first time target/stop become true (Task 4.4) within the bounded
    window - full OHLC for post-decision bars, reliable tick for the
    decision-crossing/unknown bar (Fix 4). Deterministic full re-scan each
    pass, so it is naturally idempotent."""
    if outcome.decision_action != "BUY":
        return
    if outcome.target_price is None and outcome.stop_loss is None:
        return

    rows = await _observations_for_excursion(
        session, outcome, decision_at, measurement_end
    )

    target_hit_at: datetime | None = None
    stop_hit_at: datetime | None = None
    ambiguous = False
    for row in rows:
        extremes = _extract_extremes(row, decision_at)
        if extremes is None:
            continue
        high_candidate, low_candidate = extremes
        target_hit = (
            outcome.target_price is not None and high_candidate >= outcome.target_price
        )
        stop_hit = outcome.stop_loss is not None and low_candidate <= outcome.stop_loss
        if target_hit and stop_hit and target_hit_at is None and stop_hit_at is None:
            target_hit_at = row.observed_at
            stop_hit_at = row.observed_at
            ambiguous = True
            break
        if target_hit and target_hit_at is None:
            target_hit_at = row.observed_at
        if stop_hit and stop_hit_at is None:
            stop_hit_at = row.observed_at
        if target_hit_at is not None and stop_hit_at is not None:
            break

    if ambiguous:
        outcome.target_hit_at = target_hit_at
        outcome.stop_hit_at = stop_hit_at
        outcome.target_hit_before_stop = None
        outcome.outcome_status = "AMBIGUOUS"
        return

    outcome.target_hit_at = target_hit_at
    outcome.stop_hit_at = stop_hit_at
    if target_hit_at is not None and stop_hit_at is not None:
        outcome.target_hit_before_stop = target_hit_at < stop_hit_at
    elif target_hit_at is not None:
        outcome.target_hit_before_stop = True
    elif stop_hit_at is not None:
        outcome.target_hit_before_stop = False


async def label_pending_outcomes() -> LabelerStats:
    stats = LabelerStats()
    now = datetime.now(timezone.utc)

    async with async_session_factory() as session:
        try:
            runtime_config = await build_runtime_risk_config(session)
            max_delay_seconds = await get_outcome_maximum_observation_delay_seconds(
                session
            )
            session_close_time = await get_market_session_close_time(session)
        except Exception:
            logger.exception("OUTCOME_LABELER_RUNTIME_CONFIG_FAILED")
            runtime_config = None
            max_delay_seconds = 120
            session_close_time = "18:00"

        rows = (
            (
                await session.execute(
                    select(DecisionOutcome)
                    .where(
                        DecisionOutcome.outcome_status.in_(("PENDING", "PARTIAL", "DATA_GAP"))
                    )
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
                outcome.unavailable_reason = "MISSING_DECISION_PRICE"
                continue

            decision_at = _aware(outcome.decision_at)
            eod_target_time = None
            if runtime_config is not None:
                eod_target_time = _eod_target_time(
                    decision_at, runtime_config.timezone, session_close_time
                )
            measurement_end = _measurement_end(decision_at, eod_target_time)

            updated, reasons = await _resolve_due_horizons(
                session,
                outcome,
                now=now,
                decision_at=decision_at,
                max_delay_seconds=max_delay_seconds,
                eod_target_time=eod_target_time,
            )
            stats.updated_fields += len(updated)

            await _update_mfe_mae(session, outcome, decision_at, measurement_end)
            if outcome.outcome_status != "AMBIGUOUS":
                await _update_target_stop_order(
                    session, outcome, decision_at, measurement_end
                )
                if outcome.outcome_status == "AMBIGUOUS":
                    stats.ambiguous += 1

            if outcome.outcome_status == "AMBIGUOUS":
                continue

            filled = {f: getattr(outcome, f) is not None for f in ALL_RETURN_FIELDS}
            if all(filled.values()):
                if outcome.outcome_status != "COMPLETE":
                    stats.completed += 1
                outcome.outcome_status = "COMPLETE"
                outcome.unavailable_reason = None
                continue

            # Any horizon whose target_time is old enough that it can no
            # longer plausibly resolve is a permanent gap, not "still
            # pending" - collect reasons for all such fields (Task 4.6).
            permanent_gap_reasons: list[str] = []
            for field, minutes in HORIZONS_MINUTES:
                if filled[field]:
                    continue
                target_time = decision_at + timedelta(minutes=minutes)
                if now - target_time > _HORIZON_GRACE:
                    permanent_gap_reasons.append(
                        f"{field}={reasons.get(field, 'NO_OBSERVATION_IN_WINDOW')}"
                    )
            if (
                not filled["future_return_eod"]
                and eod_target_time is not None
                and now - eod_target_time > _HORIZON_GRACE
            ):
                permanent_gap_reasons.append(
                    f"future_return_eod={reasons.get('future_return_eod', 'MARKET_CLOSED')}"
                )

            if permanent_gap_reasons:
                outcome.outcome_status = "DATA_GAP"
                outcome.unavailable_reason = "; ".join(permanent_gap_reasons)
                stats.data_gap += 1
            elif any(filled.values()):
                outcome.outcome_status = "PARTIAL"
            else:
                outcome.outcome_status = "PENDING"

        await session.commit()

    return stats


async def run_once() -> LabelerStats:
    return await label_pending_outcomes()


def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    stats = asyncio.run(run_once())
    logger.info(
        "OUTCOME_LABELER_RUN_COMPLETE processed=%s updatedFields=%s completed=%s "
        "dataGap=%s ambiguous=%s",
        stats.processed,
        stats.updated_fields,
        stats.completed,
        stats.data_gap,
        stats.ambiguous,
    )


if __name__ == "__main__":
    _main()
