"""Fill-ledger reconciliation and repair-job processing (Task 1).

Two independent jobs, both safe to run repeatedly and both scanner/order-path
independent:

1. ``run_fill_reconciliation`` - finds OrderLog rows where
   SUM(OrderFill.fill_qty) is short of OrderLog.filled_qty (a fill that was
   lost when the callback-time SAVEPOINT in order_lifecycle.apply_callback
   failed) and creates the missing OrderFill + applies it to the lifecycle.
2. ``process_repair_jobs`` - drains the MeasurementRepairJob queue created by
   that same failure path (or by future LIFECYCLE_RECONCILIATION /
   OUTCOME_RECONCILIATION producers), retrying with a capped attempt count
   before escalating to MANUAL_REVIEW.

Callable as:
    python -m app.services.measurement_reconciliation
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.db import MeasurementRepairJob, OrderLog
from app.services.fill_ledger import record_reconciliation_fill
from app.services.measurement_repair import (
    MAX_REPAIR_ATTEMPTS,
    enqueue_repair_job,
)
from app.services.position_lifecycle_engine import apply_fill_to_lifecycle
from app.services.position_lifecycle_reconciliation import reconcile_all_open_lifecycles

logger = logging.getLogger(__name__)


@dataclass
class ReconciliationStats:
    orders_checked: int = 0
    fills_recovered: int = 0
    skipped_implausible: int = 0


@dataclass
class RepairJobStats:
    processed: int = 0
    completed: int = 0
    retried: int = 0
    escalated_manual_review: int = 0


async def _candidate_orders_for_reconciliation(session) -> list[OrderLog]:
    """OrderLog rows with any recorded fill progress at all - a plain scan
    bounded to rows that could possibly have a gap. Fill-qty comparison
    itself happens per-row in find_missing_fill_gap (Decimal-precise), not
    here, since SQLite/Postgres portable SQL can't safely compare a Numeric
    column against a correlated subquery sum in one dialect-neutral query.
    """
    stmt = select(OrderLog).where(OrderLog.filled_qty > 0)
    return list((await session.execute(stmt)).scalars().all())


async def run_fill_reconciliation() -> ReconciliationStats:
    stats = ReconciliationStats()
    async with async_session_factory() as session:
        orders = await _candidate_orders_for_reconciliation(session)
        for row in orders:
            stats.orders_checked += 1
            try:
                async with session.begin_nested():
                    fill = await record_reconciliation_fill(session, row)
                    if fill is not None:
                        await apply_fill_to_lifecycle(session, row, fill)
            except Exception as exc:
                logger.exception(
                    "FILL_RECONCILIATION_FAILED request_id=%s", row.request_id
                )
                try:
                    await enqueue_repair_job(
                        session,
                        repair_type="FILL_RECONCILIATION",
                        last_error=repr(exc),
                        request_id=row.request_id,
                        order_log_id=row.id,
                        symbol=row.symbol,
                    )
                except Exception:
                    logger.exception(
                        "MEASUREMENT_REPAIR_JOB_ENQUEUE_FAILED request_id=%s",
                        row.request_id,
                    )
                continue
            if fill is not None:
                stats.fills_recovered += 1
        await session.commit()
    return stats


async def _resolve_order_log(session, job: MeasurementRepairJob) -> OrderLog | None:
    if job.order_log_id is not None:
        return await session.get(OrderLog, job.order_log_id)
    if job.request_id is not None:
        return (
            await session.execute(
                select(OrderLog).where(OrderLog.request_id == job.request_id)
            )
        ).scalar_one_or_none()
    return None


async def _process_one_repair_job(session, job: MeasurementRepairJob) -> bool:
    """Return True if the job's underlying gap was closed (or found already
    closed), False if it should be retried later."""
    if job.repair_type == "FILL_RECONCILIATION":
        row = await _resolve_order_log(session, job)
        if row is None:
            raise RuntimeError(f"order_log {job.order_log_id!r} not found")
        async with session.begin_nested():
            fill = await record_reconciliation_fill(session, row)
            if fill is not None:
                await apply_fill_to_lifecycle(session, row, fill)
        return True

    # LIFECYCLE_RECONCILIATION / OUTCOME_RECONCILIATION producers land in a
    # later task; an unrecognized-but-valid repair_type is left PENDING
    # rather than silently dropped or guessed at.
    raise RuntimeError(f"no handler implemented yet for {job.repair_type}")


async def process_repair_jobs() -> RepairJobStats:
    stats = RepairJobStats()
    now = datetime.now(timezone.utc)
    async with async_session_factory() as session:
        stmt = select(MeasurementRepairJob).where(
            MeasurementRepairJob.status.in_(("PENDING", "FAILED")),
            (MeasurementRepairJob.next_attempt_at.is_(None))
            | (MeasurementRepairJob.next_attempt_at <= now),
        )
        jobs = list((await session.execute(stmt)).scalars().all())

        for job in jobs:
            stats.processed += 1
            job.status = "PROCESSING"
            job.attempt_count += 1
            await session.flush()
            try:
                closed = await _process_one_repair_job(session, job)
            except Exception as exc:
                logger.exception(
                    "MEASUREMENT_REPAIR_JOB_ATTEMPT_FAILED id=%s type=%s attempt=%s",
                    job.id,
                    job.repair_type,
                    job.attempt_count,
                )
                job.last_error = repr(exc)[:4000]
                if job.attempt_count >= MAX_REPAIR_ATTEMPTS:
                    job.status = "MANUAL_REVIEW"
                    stats.escalated_manual_review += 1
                else:
                    job.status = "FAILED"
                    # Exponential-ish backoff, capped, so a persistently
                    # broken job does not spin the runner in a tight loop.
                    job.next_attempt_at = now + timedelta(
                        minutes=min(60, 2**job.attempt_count)
                    )
                    stats.retried += 1
                await session.flush()
                continue

            if closed:
                job.status = "COMPLETED"
                job.completed_at = now
                stats.completed += 1
            else:
                job.status = "FAILED"
                job.next_attempt_at = now + timedelta(minutes=min(60, 2**job.attempt_count))
                stats.retried += 1
            await session.flush()

        await session.commit()
    return stats


async def run_once() -> tuple[ReconciliationStats, RepairJobStats, tuple[int, int]]:
    reconciliation_stats = await run_fill_reconciliation()
    repair_stats = await process_repair_jobs()
    async with async_session_factory() as session:
        lifecycle_qty_stats = await reconcile_all_open_lifecycles(session)
    return reconciliation_stats, repair_stats, lifecycle_qty_stats


def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    reconciliation_stats, repair_stats, (checked, corrected) = asyncio.run(run_once())
    logger.info(
        "MEASUREMENT_RECONCILIATION_RUN_COMPLETE ordersChecked=%s fillsRecovered=%s "
        "skippedImplausible=%s",
        reconciliation_stats.orders_checked,
        reconciliation_stats.fills_recovered,
        reconciliation_stats.skipped_implausible,
    )
    logger.info(
        "MEASUREMENT_REPAIR_JOBS_RUN_COMPLETE processed=%s completed=%s retried=%s "
        "escalatedManualReview=%s",
        repair_stats.processed,
        repair_stats.completed,
        repair_stats.retried,
        repair_stats.escalated_manual_review,
    )
    logger.info(
        "LIFECYCLE_QTY_RECONCILIATION_RUN_COMPLETE checked=%s resolved=%s",
        checked,
        corrected,
    )


if __name__ == "__main__":
    _main()
