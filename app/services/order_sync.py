"""Reconcile and cancel exchange orders without creating new orders."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.db.session import async_session_factory
from app.models.db import OrderLog
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    MatriksGatewayClient,
    gateway_client,
)

logger = logging.getLogger(__name__)
FINAL_STATUSES = {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}
PENDING_STATUSES = {"SENT_PENDING", "NEW", "PARTIALLY_FILLED", "CANCEL_REQUESTED"}


async def reconcile_orders(gateway: MatriksGatewayClient | Any = gateway_client) -> int:
    """Apply only definitive gateway statuses to local pending rows."""
    try:
        payload = await gateway.get_active_orders()
    except (GatewayUnavailable, GatewayError):
        logger.warning("Order reconciliation skipped: gateway unavailable")
        return 0
    except Exception:
        logger.exception("Order reconciliation gateway query failed")
        return 0

    states = [row for row in payload.get("orders") or [] if isinstance(row, dict)]
    by_order_id = {str(row.get("orderId")): row for row in states if row.get("orderId")}
    by_request_id = {str(row.get("requestId")): row for row in states if row.get("requestId")}
    updated = 0
    try:
        async with async_session_factory() as session:
            rows = list((await session.execute(select(OrderLog).where(OrderLog.status.in_(PENDING_STATUSES)))).scalars().all())
            for local in rows:
                state = by_order_id.get(local.order_id or "") or by_request_id.get(local.request_id)
                if state is None:
                    candidates = [
                        item for item in states
                        if str(item.get("symbol") or "").upper() == local.symbol.upper()
                        and str(item.get("side") or "").upper() == local.action.upper()
                        and abs(float(item.get("qty") or 0) - float(local.qty)) < 1e-9
                        and abs(float(item.get("price") or 0) - float(local.price or 0)) < 1e-9
                    ]
                    if len(candidates) == 1:
                        state = candidates[0]
                if state is None:
                    continue
                if state.get("orderId") and not local.order_id:
                    local.order_id = str(state["orderId"])
                status = str(state.get("status") or "").upper()
                if status in FINAL_STATUSES:
                    local.status = "CANCELED" if status == "CANCELLED" else status
                    final_price = state.get("avgPrice") or state.get("price")
                    if final_price is not None:
                        local.price = float(final_price)
                    local.matrix_message = "order reconciliation: gateway final status"
                    updated += 1
            await session.commit()
    except Exception:
        logger.exception("Order reconciliation DB update failed")
        return 0
    return updated


async def cancel_timed_out_orders(
    gateway: MatriksGatewayClient | Any = gateway_client,
    *,
    now: datetime | None = None,
) -> int:
    """Request cancellation for stale LIMIT orders; never sends an order."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(
        minutes=settings.order_pending_timeout_minutes
    )
    try:
        async with async_session_factory() as session:
            rows = list((await session.execute(select(OrderLog).where(
                OrderLog.status.in_(("SENT_PENDING", "NEW", "PARTIALLY_FILLED")),
                OrderLog.created_at <= cutoff,
                OrderLog.order_id.is_not(None),
            ))).scalars().all())
            canceled = 0
            for row in rows:
                try:
                    outcome = await gateway.cancel_order(row.order_id)
                except (GatewayUnavailable, GatewayError) as exc:
                    logger.warning("Order cancel failed orderId=%s: %s", row.order_id, exc)
                    continue
                except Exception:
                    logger.exception("Order cancel failed orderId=%s", row.order_id)
                    continue
                if outcome.get("accepted"):
                    row.status = "CANCEL_REQUESTED"
                    row.matrix_message = "timeout cancel requested"
                    canceled += 1
            await session.commit()
            return canceled
    except Exception:
        logger.exception("Order timeout DB query failed")
        return 0


class OrderSynchronizer:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run(), name="order-synchronizer")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            await reconcile_orders()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=max(60, settings.order_sync_interval_seconds),
                )
            except asyncio.TimeoutError:
                pass


order_synchronizer = OrderSynchronizer()
