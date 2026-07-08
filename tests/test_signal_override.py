"""Tests for the manual signal-override feature (bypass AI for testing)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.main import app
from app.models.db import BotPosition, SignalOverride
from app.services.session_store import MAX_TOOL_CALLS_PER_SESSION, session_store
from app.services.signal_override import (
    SELL_ALL_SENTINEL_QTY,
    create_override,
    consume_override,
    override_to_raw_decision,
)


@pytest.fixture(autouse=True)
def _reset_db():
    async def _seed_permissive_cutoff() -> None:
        from app.services.admin_config import set_admin_config_value

        async with async_session_factory() as session:
            # Avoid flakiness from the real 17:30 cutoff default — tests
            # should pass regardless of what time they happen to run.
            await set_admin_config_value(
                session, "disableTradingAfter", "23:59", changed_by="test-setup",
            )

    asyncio.run(drop_all())
    asyncio.run(init_db())
    asyncio.run(_seed_permissive_cutoff())
    yield
    asyncio.run(drop_all())
    asyncio.run(init_db())


@pytest.fixture(autouse=True)
def _clean_sessions() -> None:
    session_store._store.clear()
    yield
    session_store._store.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_token}"}


# ── Service-level unit tests ──────────────────────────────────────────────────


class TestCreateAndConsumeOverride:
    def test_create_then_consume_returns_override(self):
        async def _run():
            async with async_session_factory() as session:
                await create_override(
                    session, "thyao", "SELL", SELL_ALL_SENTINEL_QTY,
                    reason="test", created_by="tester",
                )
            async with async_session_factory() as session:
                return await consume_override(session, "THYAO")

        override = asyncio.run(_run())
        assert override is not None
        assert override.symbol == "THYAO"
        assert override.action == "SELL"
        assert override.confidence == 100.0

    def test_consume_is_single_use(self):
        async def _run():
            async with async_session_factory() as session:
                await create_override(
                    session, "THYAO", "SELL", SELL_ALL_SENTINEL_QTY,
                    reason="test", created_by="tester",
                )
            async with async_session_factory() as session:
                first = await consume_override(session, "THYAO")
            async with async_session_factory() as session:
                second = await consume_override(session, "THYAO")
            return first, second

        first, second = asyncio.run(_run())
        assert first is not None
        assert second is None

    def test_consume_missing_returns_none(self):
        async def _run():
            async with async_session_factory() as session:
                return await consume_override(session, "NOPE")

        assert asyncio.run(_run()) is None

    def test_expired_override_is_ignored_and_cleaned_up(self):
        async def _run():
            async with async_session_factory() as session:
                row = SignalOverride(
                    symbol="THYAO",
                    action="SELL",
                    confidence=100.0,
                    qty=SELL_ALL_SENTINEL_QTY,
                    reason="expired test",
                    created_by="tester",
                    expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                )
                session.add(row)
                await session.commit()

            async with async_session_factory() as session:
                result = await consume_override(session, "THYAO")

            async with async_session_factory() as session:
                stmt = select(SignalOverride).where(SignalOverride.symbol == "THYAO")
                remaining = (await session.execute(stmt)).scalar_one_or_none()
            return result, remaining

        result, remaining = asyncio.run(_run())
        assert result is None
        assert remaining is None  # expired row deleted, not just ignored

    def test_create_override_upserts_same_symbol(self):
        async def _run():
            async with async_session_factory() as session:
                await create_override(
                    session, "THYAO", "BUY", 10.0,
                    reason="first", created_by="tester",
                )
                await create_override(
                    session, "THYAO", "SELL", SELL_ALL_SENTINEL_QTY,
                    reason="second", created_by="tester",
                )
            async with async_session_factory() as session:
                return await consume_override(session, "THYAO")

        override = asyncio.run(_run())
        assert override.action == "SELL"
        assert override.reason == "second"


class TestOverrideToRawDecision:
    def test_sell_override_shape(self):
        async def _run():
            async with async_session_factory() as session:
                await create_override(
                    session, "THYAO", "SELL", SELL_ALL_SENTINEL_QTY,
                    reason="liquidate", created_by="admin",
                )
            async with async_session_factory() as session:
                return await consume_override(session, "THYAO")

        override = asyncio.run(_run())
        raw = override_to_raw_decision(override)
        assert raw["action"] == "SELL"
        assert raw["confidence"] == 100.0
        assert raw["qty"] == SELL_ALL_SENTINEL_QTY
        assert "admin" in raw["reason"]
        assert "entry_range" not in raw

    def test_buy_override_includes_entry_range(self):
        async def _run():
            async with async_session_factory() as session:
                await create_override(
                    session, "THYAO", "BUY", 10.0,
                    reason="test buy", created_by="admin",
                    entry_min=100.0, entry_max=101.0,
                    stop_loss=98.0, target_price=106.0,
                )
            async with async_session_factory() as session:
                return await consume_override(session, "THYAO")

        override = asyncio.run(_run())
        raw = override_to_raw_decision(override)
        assert raw["entry_range"] == {"min": 100.0, "max": 101.0}
        assert raw["stop_loss"] == 98.0
        assert raw["target_price"] == 106.0


# ── Full pipeline integration tests ───────────────────────────────────────────


def _exhausted_session_id(symbol: str) -> str:
    """Create a session and burn its tool-call budget so the next
    evaluate-agent request proceeds straight to the AI/override step."""
    sess = session_store.create_session(symbol)
    for _ in range(MAX_TOOL_CALLS_PER_SESSION):
        session_store.increment_tool_call(sess.session_id)
    return sess.session_id


def _agentic_payload(symbol: str, session_id: str, mode: str) -> dict[str, Any]:
    return {
        "requestId": f"override-test-{symbol}",
        "symbol": symbol,
        "mode": mode,
        "sessionId": session_id,
        "marketData": {
            "symbol": symbol,
            "dataType": "OHLCV",
            "payload": {
                "lastPrice": 100.0, "open": 99.0, "high": 102.0, "low": 98.0,
                "volume": 1000.0, "botPositionQty": 500, "totalAccountQty": 500,
                "lockedLongTermQty": 0, "dailyTradeCount": 0,
            },
        },
    }


class TestOverrideAppliedThroughEvaluateAgent:
    def test_sell_override_produces_final_sell_with_clamped_qty(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        async def _seed():
            async with async_session_factory() as session:
                await create_override(
                    session, "THYAO", "SELL", SELL_ALL_SENTINEL_QTY,
                    reason="portfolio liquidation test", created_by="admin",
                )

        asyncio.run(_seed())
        session_id = _exhausted_session_id("THYAO")

        resp = client.post(
            "/api/signal/evaluate-agent",
            json=_agentic_payload("THYAO", session_id, "DEMO_LIVE"),
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()

        assert body["action"] == "SELL"
        assert body["symbol"] == "THYAO"
        assert body["allowOrder"] is True
        # Clamped to the request's reported botPositionQty (500), not the
        # 1e9 sentinel — proves the real RiskEngine SELL-qty clamp ran.
        assert body["qty"] == 500.0

    def test_real_live_mode_ignores_override(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        async def _seed():
            async with async_session_factory() as session:
                await create_override(
                    session, "THYAO", "SELL", SELL_ALL_SENTINEL_QTY,
                    reason="should not apply in REAL_LIVE", created_by="admin",
                )

        asyncio.run(_seed())
        session_id = _exhausted_session_id("THYAO")

        resp = client.post(
            "/api/signal/evaluate-agent",
            json=_agentic_payload("THYAO", session_id, "REAL_LIVE"),
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()

        # Mock provider (default AI_PROVIDER) always returns WAIT — proves
        # the override was NOT consumed/applied for REAL_LIVE.
        assert body["action"] == "WAIT"

        async def _check_still_pending():
            async with async_session_factory() as session:
                stmt = select(SignalOverride).where(SignalOverride.symbol == "THYAO")
                return (await session.execute(stmt)).scalar_one_or_none()

        assert asyncio.run(_check_still_pending()) is not None

    def test_no_override_falls_back_to_ai_provider(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        session_id = _exhausted_session_id("THYAO")

        resp = client.post(
            "/api/signal/evaluate-agent",
            json=_agentic_payload("THYAO", session_id, "DEMO_LIVE"),
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["action"] == "WAIT"  # mock provider default


# ── Admin route tests ──────────────────────────────────────────────────────


class TestAdminForceOverrideRoutes:
    def _login(self, client: TestClient) -> None:
        login = client.post(
            "/admin/login",
            data={"password": settings.admin_password},
            follow_redirects=False,
        )
        assert login.status_code == 303

    def test_force_override_requires_confirmation(self, client: TestClient):
        self._login(client)
        resp = client.post(
            "/admin/positions/THYAO/force-override",
            data={"action": "SELL", "confirmation": "wrong"},
        )
        assert resp.status_code == 400

        async def _check():
            async with async_session_factory() as session:
                stmt = select(SignalOverride).where(SignalOverride.symbol == "THYAO")
                return (await session.execute(stmt)).scalar_one_or_none()

        assert asyncio.run(_check()) is None

    def test_force_override_creates_row_with_correct_confirmation(
        self, client: TestClient
    ):
        self._login(client)
        resp = client.post(
            "/admin/positions/THYAO/force-override",
            data={"action": "SELL", "confirmation": "CONFIRM"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        async def _check():
            async with async_session_factory() as session:
                stmt = select(SignalOverride).where(SignalOverride.symbol == "THYAO")
                return (await session.execute(stmt)).scalar_one_or_none()

        row = asyncio.run(_check())
        assert row is not None
        assert row.action == "SELL"

    def test_force_sell_all_creates_override_per_held_position(
        self, client: TestClient
    ):
        async def _seed_positions():
            async with async_session_factory() as session:
                session.add(BotPosition(symbol="THYAO", qty=100.0))
                session.add(BotPosition(symbol="AKBNK", qty=50.0))
                session.add(BotPosition(symbol="SISE", qty=0.0))  # no position — skipped
                await session.commit()

        asyncio.run(_seed_positions())
        self._login(client)

        resp = client.post(
            "/admin/positions/force-sell-all",
            data={"reason": "liquidate all", "confirmation": "CONFIRM"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        async def _check():
            async with async_session_factory() as session:
                stmt = select(SignalOverride.symbol)
                return set((await session.execute(stmt)).scalars().all())

        symbols = asyncio.run(_check())
        assert symbols == {"THYAO", "AKBNK"}
