from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import OrderLog
from app.services.matriks_gateway import GatewayUnavailable
from app.services.order_sync import cancel_timed_out_orders, reconcile_orders


class StateGateway:
    def __init__(self, orders=None, unavailable=False):
        self.orders = orders or []
        self.unavailable = unavailable
        self.canceled = []

    async def get_active_orders(self):
        if self.unavailable:
            raise GatewayUnavailable("offline")
        return {"ok": True, "available": True, "orders": self.orders}

    async def cancel_order(self, order_id):
        self.canceled.append(order_id)
        return {"ok": True, "accepted": True, "status": "CANCEL_REQUESTED"}


async def _seed(status="SENT_PENDING", *, order_id=None, minutes_old=1):
    async with async_session_factory() as session:
        row = OrderLog(
            request_id="REQ-1", symbol="THYAO", action="BUY", qty=10,
            price=100, status=status, order_id=order_id, mode="DEMO_LIVE",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_old),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


async def _load(row_id):
    async with async_session_factory() as session:
        return await session.get(OrderLog, row_id)


def test_reconciliation_applies_definitive_status_and_order_id():
    async def run():
        await drop_all(); await init_db()
        row_id = await _seed()
        gateway = StateGateway([{
            "orderId": "ORD-1", "requestId": "REQ-1", "status": "FILLED",
            "avgPrice": 101.25,
        }])
        assert await reconcile_orders(gateway) == 1
        row = await _load(row_id)
        assert row.status == "FILLED"
        assert row.order_id == "ORD-1"
        assert row.price == 101.25
    asyncio.run(run())


def test_reconciliation_does_not_guess_missing_order_status():
    async def run():
        await drop_all(); await init_db()
        row_id = await _seed()
        assert await reconcile_orders(StateGateway([])) == 0
        assert (await _load(row_id)).status == "SENT_PENDING"
        assert await reconcile_orders(StateGateway(unavailable=True)) == 0
    asyncio.run(run())


def test_reconciliation_uses_only_unambiguous_order_shape_fallback():
    async def run():
        await drop_all(); await init_db()
        row_id = await _seed()
        gateway = StateGateway([{
            "orderId": "ORD-SHAPE", "symbol": "THYAO", "side": "BUY",
            "qty": 10, "price": 100, "status": "CANCELED",
        }])
        assert await reconcile_orders(gateway) == 1
        row = await _load(row_id)
        assert row.status == "CANCELED"
        assert row.order_id == "ORD-SHAPE"
    asyncio.run(run())


def test_timeout_requests_cancel_and_marks_row():
    async def run():
        await drop_all(); await init_db()
        row_id = await _seed(order_id="ORD-2", minutes_old=20)
        gateway = StateGateway()
        assert await cancel_timed_out_orders(gateway) == 1
        assert gateway.canceled == ["ORD-2"]
        assert (await _load(row_id)).status == "CANCEL_REQUESTED"
    asyncio.run(run())


def test_timeout_skips_fresh_or_unidentified_orders():
    async def run():
        await drop_all(); await init_db()
        old_without_id = await _seed(minutes_old=20)
        fresh_with_id = await _seed(order_id="ORD-FRESH", minutes_old=1)
        gateway = StateGateway()
        assert await cancel_timed_out_orders(gateway) == 0
        assert gateway.canceled == []
        assert (await _load(old_without_id)).status == "SENT_PENDING"
        assert (await _load(fresh_with_id)).status == "SENT_PENDING"
    asyncio.run(run())
