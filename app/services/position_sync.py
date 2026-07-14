"""Position sync — gateway'den çekilen pozisyonları ``bot_positions``'a yazar.

Eski mimaride bot pozisyonlarını sunucuya **push** ediyordu
(``POST /api/bot/positions/sync``). Full-inversion'da yön tersine döndü:
scanner her turda gateway'den pozisyonları **pull** edip bu tabloyu tazeler.

Tablo salt bir önbellek değil — admin panelinin Positions sayfası ve
"tümünü sat" acil durum akışı doğrudan buradan okuduğu için güncel kalması
operasyonel bir gerekliliktir.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable

from sqlalchemy import delete, select

from app.config import settings
from app.db.session import async_session_factory
from app.models.db import BotPosition, OrderLog
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    MatriksGatewayClient,
    gateway_client,
)
from app.services.decision_gate import decision_cache

logger = logging.getLogger(__name__)


async def sync_positions_from_gateway(gateway: MatriksGatewayClient) -> int:
    """Gateway'deki pozisyon anlık görüntüsünü ``bot_positions``'a upsert et.

    ``positionsLoaded=true`` yanıtı tam snapshot kabul edilir. Sıfır lotlu
    izleme sembolleri saklanmaz; snapshot'ta bulunmayan eski kayıtlar silinir.

    Returns:
        Yazılan/güncellenen satır sayısı. Gateway ulaşılamıyorsa veya isteği
        reddettiyse 0 (istisna fırlatmaz — tarama turunu bozmamalı).
    """
    try:
        snapshot = await gateway.get_positions()
    except (GatewayUnavailable, GatewayError) as exc:
        logger.warning("Position sync skipped: gateway error %s", exc)
        return 0

    confidence = str(snapshot.get("confidence", "UNKNOWN")).upper()
    if confidence not in {"HIGH", "MEDIUM"}:
        logger.info("Position sync skipped: gateway snapshot confidence=%s", confidence)
        return 0

    synced = 0
    try:
        async with async_session_factory() as session:
            # Bot ownership comes exclusively from cumulative ledger fills.
            # Replaying a partial/final callback cannot double count because
            # each request_id has one row and filled_qty is monotonic.
            orders = (
                (
                    await session.execute(
                        select(OrderLog).where(
                            OrderLog.status.in_(("PARTIALLY_FILLED", "FILLED"))
                        )
                    )
                )
                .scalars()
                .all()
            )
            bought_qty: dict[str, Decimal] = {}
            sold_qty: dict[str, Decimal] = {}
            bought_cost: dict[str, Decimal] = {}
            for order in orders:
                symbol = order.symbol.strip().upper()
                filled = Decimal(str(order.filled_qty or 0))
                if filled <= 0:
                    continue
                if order.action.upper() == "BUY":
                    fill_price = Decimal(
                        str(
                            order.avg_price
                            or order.rounded_limit_price
                            or order.limit_price
                            or 0
                        )
                    )
                    bought_qty[symbol] = bought_qty.get(symbol, Decimal("0")) + filled
                    bought_cost[symbol] = (
                        bought_cost.get(symbol, Decimal("0")) + filled * fill_price
                    )
                else:
                    sold_qty[symbol] = sold_qty.get(symbol, Decimal("0")) + filled

            # Ledger rows do not have a guaranteed SELECT order. Aggregate all
            # monotonic request fills first, then compute net ownership so a
            # SELL row returned before its historical BUY cannot be ignored.
            positions: dict[str, Decimal] = {}
            position_costs: dict[str, Decimal] = {}
            for symbol, total_bought in bought_qty.items():
                net_qty = max(
                    Decimal("0"),
                    total_bought - sold_qty.get(symbol, Decimal("0")),
                )
                if net_qty <= 0:
                    continue
                average_buy_cost = bought_cost[symbol] / total_bought
                positions[symbol] = net_qty
                position_costs[symbol] = average_buy_cost * net_qty
            for symbol, qty in positions.items():
                row = (
                    await session.execute(
                        select(BotPosition).where(BotPosition.symbol == symbol)
                    )
                ).scalar_one_or_none()

                if row is None:
                    total_cost = position_costs.get(symbol, Decimal("0"))
                    session.add(
                        BotPosition(
                            symbol=symbol,
                            qty=float(qty),
                            avg_price=float(total_cost / qty) if qty > 0 else None,
                            total_value=float(total_cost),
                        )
                    )
                else:
                    row.qty = float(qty)
                    total_cost = position_costs.get(symbol, Decimal("0"))
                    row.avg_price = float(total_cost / qty) if qty > 0 else None
                    row.total_value = float(total_cost)
                synced += 1
            if positions:
                await session.execute(
                    delete(BotPosition).where(BotPosition.symbol.not_in(positions))
                )
            else:
                await session.execute(delete(BotPosition))
            await session.commit()
            decision_cache.clear()
    except Exception:
        logger.exception("Failed to persist positions from gateway")
        return 0

    logger.info("Positions synced from gateway count=%d", synced)
    return synced


class PositionSynchronizer:
    """Refresh positions independently from trading and scanner controls."""

    def __init__(
        self,
        *,
        gateway: MatriksGatewayClient | Any = gateway_client,
        interval_seconds: float = 60.0,
        sync_func: Callable[
            [MatriksGatewayClient | Any], Awaitable[int]
        ] = sync_positions_from_gateway,
    ) -> None:
        self._gateway = gateway
        self._interval_seconds = interval_seconds
        self._sync_func = sync_func
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._last_attempt_at: datetime | None = None
        self._last_completed_at: datetime | None = None
        self._last_synced_count: int | None = None
        self._last_error: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def get_status(self) -> dict[str, object]:
        return {
            "enabled": settings.position_sync_enabled,
            "running": self.running,
            "intervalSeconds": self._interval_seconds,
            "lastAttemptAt": self._last_attempt_at.isoformat()
            if self._last_attempt_at
            else None,
            "lastCompletedAt": self._last_completed_at.isoformat()
            if self._last_completed_at
            else None,
            "lastSyncedCount": self._last_synced_count,
            "lastError": self._last_error,
        }

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="position-synchronizer")
        logger.info(
            "Position synchronizer started interval=%ss", self._interval_seconds
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None
        logger.info("Position synchronizer stopped")

    async def sync_once(self) -> int:
        """Run one gateway-to-DB refresh; this method cannot create orders."""
        self._last_attempt_at = datetime.now(timezone.utc)
        try:
            synced = await self._sync_func(self._gateway)
        except Exception as exc:
            self._last_error = str(exc)
            logger.exception("Position synchronizer tick failed")
            return 0
        self._last_completed_at = datetime.now(timezone.utc)
        self._last_synced_count = synced
        self._last_error = None
        return synced

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            await self.sync_once()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=max(5, self._interval_seconds)
                )
            except asyncio.TimeoutError:
                pass


position_synchronizer = PositionSynchronizer(
    interval_seconds=max(5, settings.position_sync_interval_seconds)
)
