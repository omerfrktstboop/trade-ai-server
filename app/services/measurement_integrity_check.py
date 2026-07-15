"""Read-only measurement-layer health report (Task 11).

Summarizes the fill ledger, position lifecycles, repair-job queue, and
outcome labeler's current state in one pass, for an operator or the admin
panel to inspect. Never sends orders, never touches scanner/admin-config
runtime settings, never touches REAL_LIVE behavior, and never prints
secrets or tokens - it only reads already-persisted measurement rows.

Callable as:
    python -m app.services.measurement_integrity_check
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select

from app.db.session import async_session_factory
from app.models.db import (
    BotPosition,
    DecisionOutcome,
    MeasurementRepairJob,
    OrderFill,
    OrderLog,
    PositionLifecycle,
)
from app.services.fill_ledger import to_decimal
from app.services.position_lifecycle_reconciliation import check_lifecycle_qty_integrity

logger = logging.getLogger(__name__)

_STALE_PENDING_OUTCOME_AGE = timedelta(hours=6)
_QTY_MISMATCH_EPSILON = Decimal("0.0000000001")


@dataclass
class IntegrityReport:
    order_log_cumulative_fill_total: Decimal = Decimal("0")
    order_fill_total: Decimal = Decimal("0")
    missing_fill_count: int = 0
    duplicate_open_lifecycle_symbols: list[str] = field(default_factory=list)
    lifecycle_qty_mismatch_count: int = 0
    backfilled_lifecycle_count: int = 0
    verified_lifecycle_count: int = 0
    pending_repair_job_count: int = 0
    failed_repair_job_count: int = 0
    outcome_horizon_data_gap_count: int = 0
    stale_pending_outcome_count: int = 0
    stop_guard_qty_mismatch_count: int = 0

    def to_dict(self) -> dict:
        return {
            "orderLogCumulativeFillTotal": float(self.order_log_cumulative_fill_total),
            "orderFillTotal": float(self.order_fill_total),
            "missingFillCount": self.missing_fill_count,
            "duplicateOpenLifecycleSymbols": self.duplicate_open_lifecycle_symbols,
            "lifecycleQtyMismatchCount": self.lifecycle_qty_mismatch_count,
            "backfilledLifecycleCount": self.backfilled_lifecycle_count,
            "verifiedLifecycleCount": self.verified_lifecycle_count,
            "pendingRepairJobCount": self.pending_repair_job_count,
            "failedRepairJobCount": self.failed_repair_job_count,
            "outcomeHorizonDataGapCount": self.outcome_horizon_data_gap_count,
            "stalePendingOutcomeCount": self.stale_pending_outcome_count,
            "stopGuardQtyMismatchCount": self.stop_guard_qty_mismatch_count,
        }


async def _fill_totals(session) -> tuple[Decimal, Decimal, int]:
    order_total = (
        await session.execute(
            select(func.coalesce(func.sum(OrderLog.filled_qty), 0)).where(
                OrderLog.filled_qty > 0
            )
        )
    ).scalar_one()
    fill_total = (
        await session.execute(select(func.coalesce(func.sum(OrderFill.fill_qty), 0)))
    ).scalar_one()

    recorded_by_order = dict(
        (
            await session.execute(
                select(OrderFill.order_log_id, func.sum(OrderFill.fill_qty)).group_by(
                    OrderFill.order_log_id
                )
            )
        ).all()
    )
    orders = (
        (await session.execute(select(OrderLog.id, OrderLog.filled_qty).where(OrderLog.filled_qty > 0)))
        .all()
    )
    missing_count = 0
    for order_id, filled_qty in orders:
        expected = to_decimal(filled_qty) or Decimal("0")
        recorded = to_decimal(recorded_by_order.get(order_id)) or Decimal("0")
        if recorded < expected:
            missing_count += 1

    return (
        to_decimal(order_total) or Decimal("0"),
        to_decimal(fill_total) or Decimal("0"),
        missing_count,
    )


async def _duplicate_open_lifecycle_symbols(session) -> list[str]:
    rows = (
        await session.execute(
            select(PositionLifecycle.symbol)
            .where(PositionLifecycle.status == "OPEN")
            .group_by(PositionLifecycle.symbol)
            .having(func.count() > 1)
        )
    ).all()
    return sorted({row[0] for row in rows})


async def _lifecycle_qty_mismatch_count(session, duplicate_symbols: set[str]) -> int:
    lifecycles = (
        (
            await session.execute(
                select(PositionLifecycle).where(PositionLifecycle.status == "OPEN")
            )
        )
        .scalars()
        .all()
    )
    count = 0
    for lifecycle in lifecycles:
        if lifecycle.symbol in duplicate_symbols:
            continue  # already flagged separately; qty comparison here is meaningless
        discrepancy = await check_lifecycle_qty_integrity(session, lifecycle)
        if discrepancy is not None:
            count += 1
    return count


async def _stop_guard_qty_mismatch_count(session) -> int:
    lifecycles = (
        (
            await session.execute(
                select(PositionLifecycle).where(
                    PositionLifecycle.status == "OPEN", PositionLifecycle.current_qty > 0
                )
            )
        )
        .scalars()
        .all()
    )
    bot_positions = {
        row.symbol.strip().upper(): row
        for row in (
            (await session.execute(select(BotPosition).where(BotPosition.qty > 0)))
            .scalars()
            .all()
        )
    }
    count = 0
    for lifecycle in lifecycles:
        bot_position = bot_positions.get(lifecycle.symbol.strip().upper())
        if bot_position is None or bot_position.qty is None:
            continue
        bot_qty_d = to_decimal(bot_position.qty)
        if bot_qty_d is None:
            continue
        if abs(bot_qty_d - (lifecycle.current_qty or Decimal("0"))) > _QTY_MISMATCH_EPSILON:
            count += 1
    return count


async def run_integrity_check() -> IntegrityReport:
    report = IntegrityReport()
    async with async_session_factory() as session:
        (
            report.order_log_cumulative_fill_total,
            report.order_fill_total,
            report.missing_fill_count,
        ) = await _fill_totals(session)

        report.duplicate_open_lifecycle_symbols = await _duplicate_open_lifecycle_symbols(
            session
        )
        report.lifecycle_qty_mismatch_count = await _lifecycle_qty_mismatch_count(
            session, set(report.duplicate_open_lifecycle_symbols)
        )
        report.backfilled_lifecycle_count = (
            await session.execute(
                select(func.count()).where(PositionLifecycle.is_backfilled.is_(True))
            )
        ).scalar_one()
        report.verified_lifecycle_count = (
            await session.execute(
                select(func.count()).where(PositionLifecycle.pnl_verified.is_(True))
            )
        ).scalar_one()

        repair_status_counts = dict(
            (
                await session.execute(
                    select(MeasurementRepairJob.status, func.count()).group_by(
                        MeasurementRepairJob.status
                    )
                )
            ).all()
        )
        report.pending_repair_job_count = repair_status_counts.get(
            "PENDING", 0
        ) + repair_status_counts.get("PROCESSING", 0)
        report.failed_repair_job_count = repair_status_counts.get(
            "FAILED", 0
        ) + repair_status_counts.get("MANUAL_REVIEW", 0)

        report.outcome_horizon_data_gap_count = (
            await session.execute(
                select(func.count()).where(DecisionOutcome.outcome_status == "DATA_GAP")
            )
        ).scalar_one()

        stale_cutoff = datetime.now(timezone.utc) - _STALE_PENDING_OUTCOME_AGE
        report.stale_pending_outcome_count = (
            await session.execute(
                select(func.count()).where(
                    DecisionOutcome.outcome_status == "PENDING",
                    DecisionOutcome.decision_at < stale_cutoff,
                )
            )
        ).scalar_one()

        report.stop_guard_qty_mismatch_count = await _stop_guard_qty_mismatch_count(session)

    return report


def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    report = asyncio.run(run_integrity_check())
    for key, value in report.to_dict().items():
        logger.info("MEASUREMENT_INTEGRITY_CHECK %s=%s", key, value)


if __name__ == "__main__":
    _main()
