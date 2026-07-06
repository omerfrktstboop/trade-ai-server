"""Tests for POST /api/order-result endpoint."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.main import app
from app.models.db.order_log import OrderLog


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def engine():
    """In-memory SQLite engine."""
    e = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield e
    await e.dispose()


@pytest.fixture
async def session(engine):
    """Async session for direct DB assertions."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s


# Override the session factory so the endpoint uses our test DB.
@pytest.fixture(autouse=True)
async def _patch_session(engine, monkeypatch):
    """Make endpoint use the test engine instead of production."""
    test_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(
        "app.routers.order_result.async_session_factory",
        lambda: test_factory(),
    )


@pytest.fixture
async def client():
    """Async HTTP test client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Auth helpers ─────────────────────────────────────────────────────────────

AUTH_HEADERS = {"Authorization": "Bearer dev-token-change-me"}


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_records_order_log(client: AsyncClient, session: AsyncSession):
    """Happy path — valid payload is persisted to order_logs."""
    payload = {
        "requestId": "req-001",
        "symbol": "BTCUSDT",
        "action": "BUY",
        "qty": 1.5,
        "price": 67500.0,
        "status": "FILLED",
        "matriksMessage": "Order executed successfully",
    }

    resp = await client.post("/api/order-result", json=payload, headers=AUTH_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    # Verify in DB
    result = await session.execute(
        select(OrderLog).where(OrderLog.request_id == "req-001")
    )
    row = result.scalar_one()
    assert row.symbol == "BTCUSDT"
    assert row.action == "BUY"
    assert row.qty == 1.5
    assert row.price == 67500.0
    assert row.status == "FILLED"
    assert row.order_id is None


@pytest.mark.asyncio
async def test_with_optional_order_id(client: AsyncClient, session: AsyncSession):
    """orderId is optional — endpoint still succeeds when omitted."""
    payload = {
        "requestId": "req-002",
        "symbol": "ETHUSDT",
        "action": "SELL",
        "qty": 2.0,
        "price": 3200.0,
        "status": "FILLED",
        "matriksMessage": "Sold",
        "orderId": "matriks-12345",
    }

    resp = await client.post("/api/order-result", json=payload, headers=AUTH_HEADERS)
    assert resp.status_code == 200

    result = await session.execute(
        select(OrderLog).where(OrderLog.request_id == "req-002")
    )
    row = result.scalar_one()
    assert row.order_id == "matriks-12345"


@pytest.mark.asyncio
async def test_rejects_without_token(client: AsyncClient):
    """Missing Authorization header returns 401."""
    resp = await client.post("/api/order-result", json={
        "requestId": "req-003",
        "symbol": "BTCUSDT",
        "action": "BUY",
        "qty": 1,
        "price": 65000,
        "status": "FILLED",
        "matriksMessage": "test",
    })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_rejects_bad_token(client: AsyncClient):
    """Wrong token returns 401."""
    resp = await client.post(
        "/api/order-result",
        json={
            "requestId": "req-004",
            "symbol": "BTCUSDT",
            "action": "BUY",
            "qty": 1,
            "price": 65000,
            "status": "FILLED",
            "matriksMessage": "test",
        },
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_camelcase_fields_accepted(client: AsyncClient, session: AsyncSession):
    """All camelCase aliases are correctly mapped."""
    payload = {
        "requestId": "req-camel",
        "symbol": "ADAUSDT",
        "action": "BUY",
        "qty": 100,
        "price": 0.45,
        "status": "PARTIAL",
        "matriksMessage": "Partially filled",
        "orderId": "ord-999",
    }

    resp = await client.post("/api/order-result", json=payload, headers=AUTH_HEADERS)
    assert resp.status_code == 200

    result = await session.execute(
        select(OrderLog).where(OrderLog.request_id == "req-camel")
    )
    row = result.scalar_one()
    assert row.symbol == "ADAUSDT"
    assert row.status == "PARTIAL"
    assert row.qty == 100
    assert row.order_id == "ord-999"
