"""Tests for the bot-facing tradeable-symbols and position-sync endpoints."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.main import app
from app.models.db import BotPosition


@pytest.fixture(autouse=True)
def _reset_db():
    asyncio.run(drop_all())
    asyncio.run(init_db())
    yield
    asyncio.run(drop_all())
    asyncio.run(init_db())


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_token}"}


class TestTradeableSymbols:
    def test_requires_auth(self, client: TestClient):
        resp = client.get("/api/bot/tradeable-symbols")
        assert resp.status_code == 401

    def test_returns_default_allowed_and_locked_symbols(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        resp = client.get("/api/bot/tradeable-symbols", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert "THYAO" in body["symbols"]
        assert "ASELS" in body["lockedLongTerm"]

    def test_reflects_admin_panel_edit(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        """Adding a symbol via the admin config API must show up here immediately."""
        resp = client.put(
            "/api/admin/config/allowedSymbols",
            json={"value": "THYAO,AKBNK,SISE,KCHOL,TUPRS,ASELS"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        resp = client.get("/api/bot/tradeable-symbols", headers=auth_headers)
        assert resp.status_code == 200
        assert "ASELS" in resp.json()["symbols"]


class TestPositionSync:
    def test_requires_auth(self, client: TestClient):
        resp = client.post("/api/bot/positions/sync", json={"positions": []})
        assert resp.status_code == 401

    def test_inserts_new_positions(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        resp = client.post(
            "/api/bot/positions/sync",
            json={"positions": [{"symbol": "thyao", "qty": 100.0}]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["synced"] == 1

        async def _fetch() -> Any:
            async with async_session_factory() as session:
                stmt = select(BotPosition).where(BotPosition.symbol == "THYAO")
                return (await session.execute(stmt)).scalar_one_or_none()

        row = asyncio.run(_fetch())
        assert row is not None
        assert row.qty == 100.0

    def test_updates_existing_position_qty(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        client.post(
            "/api/bot/positions/sync",
            json={"positions": [{"symbol": "THYAO", "qty": 100.0}]},
            headers=auth_headers,
        )
        resp = client.post(
            "/api/bot/positions/sync",
            json={"positions": [{"symbol": "THYAO", "qty": 50.0}]},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        async def _fetch() -> Any:
            async with async_session_factory() as session:
                stmt = select(BotPosition).where(BotPosition.symbol == "THYAO")
                return (await session.execute(stmt)).scalar_one_or_none()

        row = asyncio.run(_fetch())
        assert row.qty == 50.0

    def test_unreported_symbols_are_left_untouched(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        client.post(
            "/api/bot/positions/sync",
            json={"positions": [{"symbol": "THYAO", "qty": 100.0}]},
            headers=auth_headers,
        )
        client.post(
            "/api/bot/positions/sync",
            json={"positions": [{"symbol": "AKBNK", "qty": 20.0}]},
            headers=auth_headers,
        )

        async def _fetch(symbol: str) -> Any:
            async with async_session_factory() as session:
                stmt = select(BotPosition).where(BotPosition.symbol == symbol)
                return (await session.execute(stmt)).scalar_one_or_none()

        thyao = asyncio.run(_fetch("THYAO"))
        assert thyao is not None
        assert thyao.qty == 100.0
