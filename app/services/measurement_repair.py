"""Repair-job queue helpers shared by the callback-time failure path
(order_lifecycle.py) and the reconciliation runner (measurement_reconciliation.py).

A callback-time measurement failure must never block the authoritative
OrderLog commit (see order_lifecycle.apply_callback's SAVEPOINT isolation),
but it must not be silently forgotten either - this module is the queue that
turns "logged and lost" into "logged and retried" (Task 1.2).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import MeasurementRepairJob

logger = logging.getLogger(__name__)

REPAIR_TYPES = ("FILL_RECONCILIATION", "LIFECYCLE_RECONCILIATION", "OUTCOME_RECONCILIATION")
OPEN_STATUSES = ("PENDING", "PROCESSING", "FAILED")
MAX_REPAIR_ATTEMPTS = 8


async def enqueue_repair_job(
    session: AsyncSession,
    *,
    repair_type: str,
    last_error: str,
    request_id: str | None = None,
    order_log_id: int | None = None,
    symbol: str | None = None,
) -> MeasurementRepairJob:
    """Create (or reactivate) exactly one open repair job per
    (order_log_id, repair_type) pair, so a repeatedly-failing callback does
    not pile up duplicate queue entries.
    """
    if repair_type not in REPAIR_TYPES:
        raise ValueError(f"Unknown repair_type: {repair_type}")

    existing: MeasurementRepairJob | None = None
    if order_log_id is not None:
        existing = (
            await session.execute(
                select(MeasurementRepairJob).where(
                    MeasurementRepairJob.order_log_id == order_log_id,
                    MeasurementRepairJob.repair_type == repair_type,
                    MeasurementRepairJob.status.in_(OPEN_STATUSES),
                )
            )
        ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if existing is not None:
        existing.last_error = last_error[:4000]
        existing.status = "PENDING"
        existing.next_attempt_at = now
        await session.flush()
        return existing

    job = MeasurementRepairJob(
        request_id=request_id,
        order_log_id=order_log_id,
        symbol=symbol.strip().upper() if symbol else None,
        repair_type=repair_type,
        status="PENDING",
        attempt_count=0,
        last_error=last_error[:4000],
        next_attempt_at=now,
    )
    session.add(job)
    await session.flush()
    logger.warning(
        "MEASUREMENT_REPAIR_JOB_CREATED repairType=%s requestId=%s orderLogId=%s",
        repair_type,
        request_id,
        order_log_id,
    )
    return job
