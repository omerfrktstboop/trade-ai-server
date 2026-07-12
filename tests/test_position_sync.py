"""Tests for gateway → bot_positions sync (app/services/position_sync.py).

Bu tablo admin panelinin Positions sayfasını ve acil "tümünü sat" akışını
besliyor; eski push endpoint'i (/api/bot/positions/sync) kaldırıldığı için
scanner'ın pull ettiği bu yol tek veri kaynağı.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import select

from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import BotPosition, OrderLog
from app.services.matriks_gateway import MatriksGatewayClient
from app.services.position_sync import sync_positions_from_gateway
from tests.fake_gateway import FakeGateway


@pytest.fixture(autouse=True)
def _reset_db():
    asyncio.run(drop_all())
    asyncio.run(init_db())
    yield
    asyncio.run(drop_all())
    asyncio.run(init_db())


def make_client(fake: FakeGateway) -> MatriksGatewayClient:
    return MatriksGatewayClient(
        base_url="http://fake-gateway", token=fake.token, transport=fake.transport
    )


async def _positions() -> dict[str, float]:
    async with async_session_factory() as session:
        rows = (await session.execute(select(BotPosition))).scalars().all()
        return {row.symbol: row.qty for row in rows}


async def _seed_fill(request_id: str, symbol: str, side: str, qty: float) -> None:
    async with async_session_factory() as session:
        session.add(OrderLog(request_id=request_id, symbol=symbol, action=side, qty=qty, filled_qty=qty, status="FILLED"))
        await session.commit()


class TestSyncPositions:
    async def test_inserts_positions_from_gateway(self):
        fake = FakeGateway()
        await _seed_fill("buy-akbnk", "AKBNK", "BUY", 25)

        synced = await sync_positions_from_gateway(make_client(fake))

        assert synced == 1
        assert await _positions() == {"AKBNK": 25.0}

    async def test_updates_existing_rows(self):
        fake = FakeGateway()
        client = make_client(fake)
        await _seed_fill("buy-akbnk", "AKBNK", "BUY", 99)
        await sync_positions_from_gateway(client)

        fake.positions = [
            {"symbol": "AKBNK", "botQty": 99.0, "lockedLongTermQty": 0.0, "totalQty": 99.0}
        ]
        synced = await sync_positions_from_gateway(client)

        assert synced == 1
        # Tam snapshot'ta bulunmayan THYAO artık stale kabul edilip silinir.
        assert await _positions() == {"AKBNK": 99.0}

    async def test_positions_not_loaded_is_skipped(self):
        fake = FakeGateway()
        fake.positions_loaded = False

        synced = await sync_positions_from_gateway(make_client(fake))

        assert synced == 0
        assert await _positions() == {}

    async def test_gateway_unavailable_returns_zero(self):
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = MatriksGatewayClient(
            base_url="http://fake-gateway",
            token="x",
            transport=httpx.MockTransport(refuse),
        )

        # Tarama turunu bozmamalı — istisna fırlatmadan 0 döner
        assert await sync_positions_from_gateway(client) == 0

    async def test_gateway_bot_qty_alias_is_ignored(self):
        fake = FakeGateway()
        fake.positions = [
            {"symbol": "  ", "botQty": 5.0},
            {"symbol": "thyao", "botQty": 12.0},
        ]

        synced = await sync_positions_from_gateway(make_client(fake))

        assert synced == 0
        assert await _positions() == {}

    async def test_closed_and_stale_positions_are_removed(self):
        fake = FakeGateway()
        client = make_client(fake)
        await _seed_fill("buy-akbnk", "AKBNK", "BUY", 25)
        await sync_positions_from_gateway(client)

        fake.positions = [
            {"symbol": "AKBNK", "botQty": 0.0, "lockedLongTermQty": 0.0, "totalQty": 0.0}
        ]
        async with async_session_factory() as session:
            row = (await session.execute(select(OrderLog).where(OrderLog.request_id == "buy-akbnk"))).scalar_one()
            row.action = "SELL"
            await session.commit()
        synced = await sync_positions_from_gateway(client)

        assert synced == 0
        assert await _positions() == {}
