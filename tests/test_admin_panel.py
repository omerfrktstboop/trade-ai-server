"""Tests for Admin Panel MVP."""

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
from app.models.db import AiDecision, ConfigAuditLog, MarketSnapshot, OrderLog, RiskDecision


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


def _signal_payload(**kwargs: Any) -> dict[str, Any]:
    payload = {
        "requestId": "admin-test-signal",
        "symbol": "THYAO",
        "timeframe": "1h",
        "mode": "LIVE",
        "lastPrice": 100.0,
        "open": 99.0,
        "high": 101.0,
        "low": 98.0,
        "volume": 1000.0,
    }
    payload.update(kwargs)
    return payload


class TestAdminAuth:
    def test_admin_dashboard_requires_auth(self, client: TestClient):
        resp = client.get("/admin")

        assert resp.status_code == 401

    def test_admin_login_cookie_allows_dashboard(self, client: TestClient):
        login = client.post(
            "/admin/login",
            data={"password": settings.admin_password},
            follow_redirects=False,
        )

        assert login.status_code == 303
        resp = client.get("/admin")
        assert resp.status_code == 200
        assert "Dashboard" in resp.text

    def test_admin_api_requires_auth(self, client: TestClient):
        resp = client.get("/api/admin/config")

        assert resp.status_code == 401


class TestAdminConfig:
    def test_config_api_does_not_expose_secrets(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        resp = client.get("/api/admin/config", headers=auth_headers)

        assert resp.status_code == 200
        keys = {item["key"] for item in resp.json()}
        assert "API_TOKEN" not in keys
        assert "DEEPSEEK_API_KEY" not in keys
        assert "DATABASE_URL" not in keys
        descriptions = {item["key"]: item["description"] for item in resp.json()}
        assert "İşlem yapılmasına izin verilen semboller" in descriptions[
            "allowedSymbols"
        ]

    def test_config_update_writes_audit_log(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        resp = client.put(
            "/api/admin/config/maxDailyTradeCount",
            headers=auth_headers,
            json={"value": 7, "reason": "raise test limit"},
        )

        assert resp.status_code == 200
        assert resp.json()["value"] == "7"

        async def _load_audit() -> ConfigAuditLog | None:
            async with async_session_factory() as session:
                stmt = select(ConfigAuditLog).where(
                    ConfigAuditLog.key == "maxDailyTradeCount"
                )
                return (await session.execute(stmt)).scalar_one_or_none()

        audit = asyncio.run(_load_audit())
        assert audit is not None
        assert audit.old_value == "3"
        assert audit.new_value == "7"
        assert audit.reason == "raise test limit"

    @pytest.mark.parametrize("mode", ["LIVE", "DEMO_LIVE", "REAL_LIVE"])
    def test_live_modes_require_confirmation(
        self, client: TestClient, auth_headers: dict[str, str], mode: str
    ):
        resp = client.put(
            "/api/admin/config/tradingMode",
            headers=auth_headers,
            json={"value": mode, "reason": "test live"},
        )

        assert resp.status_code == 400
        assert "requires confirmation" in resp.json()["detail"]

    def test_config_page_renders(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        resp = client.get("/admin/config", headers=auth_headers)

        assert resp.status_code == 200
        assert "allowedSymbols" in resp.text
        assert "killSwitchEnabled" in resp.text
        assert "Açıklama" in resp.text
        assert "İşlem yapılmasına izin verilen semboller" in resp.text


class TestKillSwitchIntegration:
    def test_kill_switch_blocks_signal_evaluate(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        update = client.put(
            "/api/admin/config/killSwitchEnabled",
            headers=auth_headers,
            json={"value": True, "reason": "safety test"},
        )
        assert update.status_code == 200

        resp = client.post(
            "/api/signal/evaluate",
            headers=auth_headers,
            json=_signal_payload(),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "WAIT"
        assert data["allowOrder"] is False
        assert "Kill switch enabled" in data["reason"]


class TestPositionsWatchlist:
    def _login(self, client: TestClient) -> None:
        login = client.post(
            "/admin/login",
            data={"password": settings.admin_password},
            follow_redirects=False,
        )
        assert login.status_code == 303

    def test_position_outside_allowed_symbols_shows_add_button(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        client.post(
            "/api/bot/positions/sync",
            headers=auth_headers,
            json={"positions": [{"symbol": "ASELS", "qty": 10.0}]},
        )
        self._login(client)

        resp = client.get("/admin/positions")
        assert resp.status_code == 200
        assert "İzleme listesinde değil" in resp.text
        assert "add-to-watchlist" in resp.text

    def test_add_to_watchlist_updates_allowed_symbols(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        client.post(
            "/api/bot/positions/sync",
            headers=auth_headers,
            json={"positions": [{"symbol": "ASELS", "qty": 10.0}]},
        )
        self._login(client)

        resp = client.post(
            "/admin/positions/add-to-watchlist",
            data={"symbol": "asels"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        config = client.get("/api/admin/config", headers=auth_headers)
        allowed = next(
            item for item in config.json() if item["key"] == "allowedSymbols"
        )
        assert "ASELS" in allowed["value"]


class TestLogDetailView:
    def _login(self, client: TestClient) -> None:
        login = client.post(
            "/admin/login",
            data={"password": settings.admin_password},
            follow_redirects=False,
        )
        assert login.status_code == 303

    async def _seed(self, request_id: str) -> None:
        async with async_session_factory() as session:
            session.add(MarketSnapshot(
                request_id=request_id, symbol="THYAO", timeframe="1h",
                open=99.0, high=102.0, low=98.0, close=100.0, volume=1000.0,
                rsi=45.0, ema20=98.5, ema50=97.0, macd=0.1, macd_signal=0.05,
                mode="DEMO_LIVE",
            ))
            session.add(AiDecision(
                request_id=request_id, symbol="THYAO", provider="deepseek",
                raw_request={"symbol": "THYAO", "rsi": 45.0},
                raw_response={"action": "SELL", "confidence": 82, "reason": "bearish"},
                action="SELL", confidence=82.0, qty=300.0, reason="bearish",
            ))
            session.add(RiskDecision(
                request_id=request_id, symbol="THYAO", action="SELL",
                confidence=82.0, risk_score=10.0, allow_order=True,
                reason="RiskEngine approved", qty=300.0, order_type="LIMIT",
                mode="DEMO_LIVE",
            ))
            session.add(OrderLog(
                request_id=request_id, symbol="THYAO", action="SELL",
                qty=300.0, price=41.6, status="FILLED", mode="DEMO_LIVE",
                matrix_message="Order accepted",
            ))
            await session.commit()

    def test_requires_auth(self, client: TestClient):
        resp = client.get("/admin/logs/some-request-id")
        assert resp.status_code == 401

    def test_shows_full_pipeline_for_matching_request_id(self, client: TestClient):
        asyncio.run(self._seed("req-detail-1"))
        self._login(client)

        resp = client.get("/admin/logs/req-detail-1")
        assert resp.status_code == 200
        assert "req-detail-1" in resp.text
        # Market snapshot values
        assert "45.0" in resp.text
        # Raw AI payload/response JSON rendered
        assert "bearish" in resp.text
        # Risk decision
        assert "RiskEngine approved" in resp.text
        # Order log
        assert "Order accepted" in resp.text
        assert "FILLED" in resp.text

    def test_missing_request_id_shows_empty_states(self, client: TestClient):
        self._login(client)
        resp = client.get("/admin/logs/does-not-exist")
        assert resp.status_code == 200
        assert "No AI decision found" in resp.text
        assert "No risk decision found" in resp.text

    def test_logs_page_links_to_detail_view(self, client: TestClient):
        asyncio.run(self._seed("req-detail-2"))
        self._login(client)

        resp = client.get("/admin/logs")
        assert resp.status_code == 200
        assert "/admin/logs/req-detail-2" in resp.text
