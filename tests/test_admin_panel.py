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
from app.models.db import ConfigAuditLog


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
