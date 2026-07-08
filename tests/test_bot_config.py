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


class TestBotRuntimeConfig:
    def test_requires_auth(self, client: TestClient):
        resp = client.get("/api/bot/config")
        assert resp.status_code == 401

    def test_returns_default_runtime_config(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        resp = client.get("/api/bot/config", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()

        assert body["configVersion"]
        assert body["configHash"]
        assert body["mode"] == "PAPER"
        assert body["allowMarketOrders"] is False
        assert body["orderTimeInForce"] == "Day"
        assert body["indicatorPeriod"] == "Min5"
        assert "THYAO" in body["allowedSymbols"]
        assert isinstance(body["lockedLongTermQty"], dict)
        assert body["activeTradeProfile"]["code"] == "NORMAL"

    def test_reflects_admin_config_values(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        update = client.put(
            "/api/admin/config/allowedSymbols",
            json={"value": "THYAO,AKBNK,ASELS"},
            headers=auth_headers,
        )
        assert update.status_code == 200

        resp = client.get("/api/bot/config", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["allowedSymbols"] == ["THYAO", "AKBNK", "ASELS"]

    def test_hash_changes_when_config_changes(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        """scanIntervalMinutes is now trade-profile-driven (see
        tests/test_trade_profiles.py) — botHttpTimeoutSeconds remains a
        standalone admin config key, so it's used here to prove the hash
        still reacts to non-profile config changes."""
        before = client.get("/api/bot/config", headers=auth_headers).json()

        update = client.put(
            "/api/admin/config/botHttpTimeoutSeconds",
            json={"value": 45, "reason": "test slower timeout"},
            headers=auth_headers,
        )
        assert update.status_code == 200

        after = client.get("/api/bot/config", headers=auth_headers).json()
        assert after["httpTimeoutSeconds"] == 45
        assert after["configHash"] != before["configHash"]

    def test_market_orders_cannot_be_enabled(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        resp = client.put(
            "/api/admin/config/botAllowMarketOrders",
            json={"value": True, "reason": "must stay disabled"},
            headers=auth_headers,
        )

        assert resp.status_code == 400
        assert "MARKET orders are disabled" in resp.json()["detail"]

    def test_risky_bot_live_mode_requires_confirmation(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        resp = client.put(
            "/api/admin/config/botMode",
            json={"value": "DEMO_LIVE", "reason": "demo test"},
            headers=auth_headers,
        )

        assert resp.status_code == 400
        assert "requires confirmation" in resp.json()["detail"]


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
