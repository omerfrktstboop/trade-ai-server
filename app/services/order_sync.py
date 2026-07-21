"""Reconcile and cancel exchange orders without creating new orders."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.db.session import async_session_factory
from app.models.db import OrderLog
from app.services.order_lifecycle import apply_callback
from app.services.order_ledger import PENDING_STATES
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    MatriksGatewayClient,
    gateway_client,
)

logger = logging.getLogger(__name__)
FINAL_STATUSES = {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}
PENDING_STATUSES = set(PENDING_STATES)


def _finite_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


async def reconcile_orders(gateway: MatriksGatewayClient | Any = gateway_client) -> int:
    """Reconcile pending rows through the authoritative callback/fill path."""
    try:
        payload = await gateway.get_active_orders()
    except (GatewayUnavailable, GatewayError):
        logger.warning("Order reconciliation skipped: gateway unavailable")
        return 0
    except Exception:
        logger.exception("Order reconciliation gateway query failed")
        return 0

    states = [row for row in payload.get("orders") or [] if isinstance(row, dict)]
    by_order_id = {
        str(row.get("orderId")): row for row in states if row.get("orderId")
    }
    by_request_id = {
        str(row.get("requestId")): row for row in states if row.get("requestId")
    }
    updated = 0
    try:
        async with async_session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(OrderLog).where(OrderLog.status.in_(PENDING_STATUSES))
                    )
                )
                .scalars()
                .all()
            )
            for local in rows:
                state = by_order_id.get(local.order_id or "") or by_request_id.get(
                    local.request_id
                )
                if state is None:
                    continue
                status = str(state.get("status") or "").upper()
                if status not in FINAL_STATUSES | PENDING_STATUSES:
                    continue
                order_qty = _finite_decimal(state.get("qty"))
                filled_qty = _finite_decimal(state.get("filledQty"))
                avg_price = _finite_decimal(state.get("avgPrice"))
                limit_price = _finite_decimal(state.get("price"))
                if (
                    order_qty is None
                    or order_qty <= 0
                    or order_qty != order_qty.to_integral_value()
                    or filled_qty is None
                    or filled_qty < 0
                    or filled_qty > order_qty
                ):
                    logger.error(
                        "Order reconciliation rejected invalid quantities requestId=%s",
                        local.request_id,
                    )
                    continue
                if status == "FILLED" and filled_qty != order_qty:
                    logger.error(
                        "Order reconciliation refused FILLED without full cumulative qty requestId=%s",
                        local.request_id,
                    )
                    continue
                if filled_qty > 0 and (avg_price is None or avg_price <= 0):
                    logger.error(
                        "Order reconciliation refused fill without average price requestId=%s",
                        local.request_id,
                    )
                    continue
                _, changed = await apply_callback(
                    session,
                    request_id=local.request_id,
                    symbol=local.symbol,
                    action=local.action,
                    status="CANCELED" if status == "CANCELLED" else status,
                    order_qty=float(order_qty),
                    filled_qty=float(filled_qty),
                    last_fill_qty=float(
                        max(Decimal("0"), filled_qty - Decimal(str(local.filled_qty or 0)))
                    ),
                    avg_price=float(avg_price) if avg_price is not None else None,
                    limit_price=(
                        float(limit_price) if limit_price is not None else local.limit_price
                    ),
                    order_id=str(state.get("orderId") or local.order_id or "") or None,
                    message="order reconciliation: gateway cumulative state",
                )
                if changed:
                    updated += 1
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
            rows = list(
                (
                    await session.execute(
                        select(OrderLog).where(
                            OrderLog.status.in_(
                                ("SENT_PENDING", "NEW", "PARTIALLY_FILLED")
                            ),
                            OrderLog.created_at <= cutoff,
                            OrderLog.order_id.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            canceled = 0
            for row in rows:
                try:
                    outcome = await gateway.cancel_order(row.order_id)
                except (GatewayUnavailable, GatewayError) as exc:
                    logger.warning(
                        "Order cancel failed orderId=%s: %s", row.order_id, exc
                    )
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
