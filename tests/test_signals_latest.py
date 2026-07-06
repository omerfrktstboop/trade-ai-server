"""Tests for GET /api/signals/latest endpoint."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.main import app
from app.models.db import RiskDecision

AUTH_HEADERS = {"Authorization": "Bearer dev-token-change-me"}
BASE_URL = "/api/signals/latest"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _reset_db():
    """Drop and recreate all tables before each test."""
    await drop_all()
    await init_db()


@pytest.fixture
async def client():
    """Async HTTP test client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def db_session():
    """Raw async session for seeding test data."""
    async with async_session_factory() as session:
        yield session


async def _seed_decisions(db: AsyncSession, records: list[dict]) -> None:
    """Insert raw risk decisions into the database."""
    for r in records:
        db.add(RiskDecision(**r))
    await db.commit()


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestLatestSignals:
    """GET /api/signals/latest — core behaviour."""

    async def test_no_auth_returns_401(self, client: AsyncClient):
        """Missing token => 401."""
        resp = await client.get(BASE_URL)
        assert resp.status_code == 401

    async def test_wrong_token_returns_401(self, client: AsyncClient):
        """Wrong token => 401."""
        resp = await client.get(BASE_URL, headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    async def test_empty_db_returns_empty_list(self, client: AsyncClient):
        """No decisions => []."""
        resp = await client.get(BASE_URL, headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_max_20(self, db_session: AsyncSession, client: AsyncClient):
        """Seed 25 records, expect 20 returned."""
        await _seed_decisions(
            db_session,
            [
                {
                    "request_id": f"req-{i}",
                    "symbol": f"S{i % 3:03d}",
                    "action": "WAIT",
                    "confidence": 50.0,
                    "risk_score": 0.0,
                    "allow_order": False,
                    "reason": f"test {i}",
                    "order_type": "NONE",
                    "qty": 0,
                    "mode": "PAPER",
                }
                for i in range(25)
            ],
        )

        resp = await client.get(BASE_URL, headers=AUTH_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 20

    async def test_camelcase_keys(self, db_session: AsyncSession, client: AsyncClient):
        """Every item uses camelCase field names."""
        await _seed_decisions(
            db_session,
            [
                {
                    "request_id": "req-camel",
                    "symbol": "THYAO",
                    "action": "WAIT",
                    "confidence": 60.0,
                    "risk_score": 10.0,
                    "allow_order": False,
                    "reason": "camel test",
                    "order_type": "NONE",
                    "qty": 0,
                    "mode": "PAPER",
                }
            ],
        )

        resp = await client.get(BASE_URL, headers=AUTH_HEADERS)
        item = resp.json()[0]

        assert "requestId" in item
        assert "confidenceScore" in item
        assert "riskScore" in item
        assert "allowOrder" in item
        assert "orderType" in item
        assert "entryMin" in item
        assert "entryMax" in item
        assert "stopLoss" in item
        assert "targetPrice" in item
        assert "createdAt" in item
        # No snake_case leaks
        assert "request_id" not in item
        assert "confidence" not in item

    async def test_newest_first(self, db_session: AsyncSession, client: AsyncClient):
        """Results ordered by created_at DESC."""
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        await _seed_decisions(
            db_session,
            [
                {
                    "request_id": "oldest",
                    "symbol": "THYAO",
                    "action": "WAIT",
                    "confidence": 10.0,
                    "risk_score": 0.0,
                    "allow_order": False,
                    "reason": "old",
                    "order_type": "NONE",
                    "qty": 0,
                    "mode": "PAPER",
                    "created_at": now - timedelta(hours=2),
                },
                {
                    "request_id": "newest",
                    "symbol": "THYAO",
                    "action": "WAIT",
                    "confidence": 90.0,
                    "risk_score": 0.0,
                    "allow_order": False,
                    "reason": "new",
                    "order_type": "NONE",
                    "qty": 0,
                    "mode": "PAPER",
                    "created_at": now,
                },
            ],
        )

        resp = await client.get(BASE_URL, headers=AUTH_HEADERS)
        data = resp.json()
        assert data[0]["requestId"] == "newest"
        assert data[1]["requestId"] == "oldest"


class TestLatestSignalsSymbolFilter:
    """GET /api/signals/latest?symbol=..."""

    async def test_filter_exact_match(self, db_session: AsyncSession, client: AsyncClient):
        """?symbol=THYAO returns only THYAO records."""
        await _seed_decisions(
            db_session,
            [
                {"request_id": "r1", "symbol": "THYAO", "action": "BUY", "confidence": 80.0,
                 "risk_score": 15.0, "allow_order": True, "reason": "buy thyao",
                 "order_type": "LIMIT", "qty": 10, "mode": "LIVE"},
                {"request_id": "r2", "symbol": "AKBNK", "action": "WAIT", "confidence": 50.0,
                 "risk_score": 0.0, "allow_order": False, "reason": "wait akbnk",
                 "order_type": "NONE", "qty": 0, "mode": "PAPER"},
                {"request_id": "r3", "symbol": "THYAO", "action": "SELL", "confidence": 75.0,
                 "risk_score": 20.0, "allow_order": True, "reason": "sell thyao",
                 "order_type": "MARKET", "qty": 5, "mode": "LIVE"},
            ],
        )

        resp = await client.get(f"{BASE_URL}?symbol=THYAO", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        symbols = {r["symbol"] for r in data}
        assert symbols == {"THYAO"}

    async def test_filter_case_insensitive(self, db_session: AsyncSession, client: AsyncClient):
        """?symbol=thyao (lowercase) still matches THYAO."""
        await _seed_decisions(
            db_session,
            [
                {"request_id": "r1", "symbol": "THYAO", "action": "WAIT", "confidence": 50.0,
                 "risk_score": 0.0, "allow_order": False, "reason": "test",
                 "order_type": "NONE", "qty": 0, "mode": "PAPER"},
            ],
        )

        resp = await client.get(f"{BASE_URL}?symbol=thyao", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["symbol"] == "THYAO"

    async def test_filter_no_match(self, db_session: AsyncSession, client: AsyncClient):
        """?symbol=NONEXIST returns []."""
        await _seed_decisions(
            db_session,
            [
                {"request_id": "r1", "symbol": "THYAO", "action": "WAIT", "confidence": 50.0,
                 "risk_score": 0.0, "allow_order": False, "reason": "test",
                 "order_type": "NONE", "qty": 0, "mode": "PAPER"},
            ],
        )

        resp = await client.get(f"{BASE_URL}?symbol=XXXXX", headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.json() == []
