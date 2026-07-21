"""Tests for the trade profiles (risk profiles) system."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.main import app
from app.models.db import ConfigAuditLog, TradeProfile
from app.services.trade_profile import (
    BUILTIN_PROFILES,
    EDITABLE_FIELDS,
    activate_profile,
    clone_profile,
    create_profile,
    delete_profile,
    disable_profile,
    get_active_profile,
    get_profile,
    list_profiles,
    update_profile,
)


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


def _run(coro):
    return asyncio.run(coro)


# ── Seeding ────────────────────────────────────────────────────────────────


class TestBuiltinSeed:
    def test_four_builtin_profiles_seeded(self):
        async def _check():
            async with async_session_factory() as session:
                return await list_profiles(session)

        profiles = _run(_check())
        codes = {p.code for p in profiles}
        assert codes == {"CONSERVATIVE", "NORMAL", "AGGRESSIVE", "HIGH_RISK"}
        assert all(p.is_builtin for p in profiles)

    def test_normal_is_the_default(self):
        async def _check():
            async with async_session_factory() as session:
                return await get_profile(session, "NORMAL")

        normal = _run(_check())
        assert normal.is_default is True

    def test_get_active_profile_defaults_to_normal(self):
        async def _check():
            async with async_session_factory() as session:
                return await get_active_profile(session)

        active = _run(_check())
        assert active.code == "NORMAL"

    def test_seeding_is_idempotent(self):
        async def _check():
            async with async_session_factory() as session:
                await list_profiles(session)  # triggers seed
                await list_profiles(session)  # should be a no-op the 2nd time
                stmt = select(TradeProfile.code)
                return (await session.execute(stmt)).scalars().all()

        codes = _run(_check())
        assert len(codes) == 4


# ── CRUD ─────────────────────────────────────────────────────────────────────


class TestCreateUpdateProfile:
    def test_create_profile(self):
        async def _run_create():
            async with async_session_factory() as session:
                return await create_profile(
                    session,
                    code="my_custom",
                    name="My Custom",
                    changed_by="tester",
                    **{
                        k: v
                        for k, v in BUILTIN_PROFILES["NORMAL"].items()
                        if k not in ("name", "description", "risk_level", "is_default")
                    },
                )

        profile = _run(_run_create())
        assert profile.code == "MY_CUSTOM"
        assert profile.is_builtin is False

    def test_create_duplicate_code_fails(self):
        async def _run_dup():
            async with async_session_factory() as session:
                await create_profile(
                    session,
                    code="NORMAL",
                    name="dupe",
                    changed_by="tester",
                    **{
                        k: v
                        for k, v in BUILTIN_PROFILES["NORMAL"].items()
                        if k not in ("name", "description", "risk_level", "is_default")
                    },
                )

        with pytest.raises(ValueError, match="already exists"):
            _run(_run_dup())

    def test_update_profile_field(self):
        async def _run_update():
            async with async_session_factory() as session:
                return await update_profile(
                    session,
                    "CONSERVATIVE",
                    {"description": "updated desc"},
                    changed_by="tester",
                )

        profile = _run(_run_update())
        assert profile.description == "updated desc"

    def test_update_unknown_field_rejected(self):
        async def _run_update():
            async with async_session_factory() as session:
                await update_profile(
                    session, "NORMAL", {"not_a_field": 1}, changed_by="tester"
                )

        with pytest.raises(ValueError, match="Unknown"):
            _run(_run_update())


class TestUpdateAudit:
    def test_risky_update_succeeds_directly_and_keeps_audit(self):
        async def _run_update():
            async with async_session_factory() as session:
                profile = await update_profile(
                    session,
                    "NORMAL",
                    {"max_order_value_tl": 999999},
                    changed_by="tester",
                    reason="approved limit change",
                )
                audit = (
                    await session.execute(
                        select(ConfigAuditLog).where(
                            ConfigAuditLog.key == "trade_profile:NORMAL"
                        )
                    )
                ).scalar_one()
                return profile, audit

        profile, audit = _run(_run_update())
        assert profile.max_order_value_tl == 999999
        assert audit.changed_by == "tester"
        assert audit.reason == "approved limit change"


# ── Clone / disable / delete ────────────────────────────────────────────────


class TestCloneDisableDelete:
    def test_clone_builtin_profile(self):
        async def _run_clone():
            async with async_session_factory() as session:
                return await clone_profile(
                    session,
                    "AGGRESSIVE",
                    new_code="AGGRESSIVE_V2",
                    new_name="Aggressive v2",
                    changed_by="tester",
                )

        clone = _run(_run_clone())
        assert clone.is_builtin is False
        assert (
            clone.max_order_value_tl
            == BUILTIN_PROFILES["AGGRESSIVE"]["max_order_value_tl"]
        )

    def test_delete_builtin_profile_rejected(self):
        async def _run_delete():
            async with async_session_factory() as session:
                await delete_profile(session, "CONSERVATIVE", changed_by="tester")

        with pytest.raises(ValueError, match="cannot be deleted"):
            _run(_run_delete())

    def test_delete_custom_profile_succeeds(self):
        async def _run_flow():
            async with async_session_factory() as session:
                await clone_profile(
                    session,
                    "CONSERVATIVE",
                    new_code="TEMP",
                    new_name="Temp",
                    changed_by="tester",
                )
            async with async_session_factory() as session:
                await delete_profile(session, "TEMP", changed_by="tester")
            async with async_session_factory() as session:
                return await get_profile(session, "TEMP")

        assert _run(_run_flow()) is None

    def test_disable_default_profile_rejected(self):
        async def _run_disable():
            async with async_session_factory() as session:
                await disable_profile(session, "NORMAL", changed_by="tester")

        with pytest.raises(ValueError, match="default"):
            _run(_run_disable())

    def test_disable_active_profile_rejected(self):
        async def _run_flow():
            async with async_session_factory() as session:
                await activate_profile(session, "CONSERVATIVE", changed_by="tester")
            async with async_session_factory() as session:
                await disable_profile(session, "CONSERVATIVE", changed_by="tester")

        with pytest.raises(ValueError, match="active"):
            _run(_run_flow())

    def test_disable_non_active_non_default_profile_succeeds(self):
        async def _run_flow():
            async with async_session_factory() as session:
                profile = await disable_profile(
                    session, "HIGH_RISK", changed_by="tester"
                )
            return profile

        profile = _run(_run_flow())
        assert profile.is_enabled is False


# ── Activation ────────────────────────────────────────────────────────────


class TestActivation:
    def test_activate_low_risk_profile(self):
        async def _run_activate():
            async with async_session_factory() as session:
                return await activate_profile(
                    session, "CONSERVATIVE", changed_by="tester"
                )

        profile = _run(_run_activate())
        assert profile.code == "CONSERVATIVE"

    def test_activate_high_risk_succeeds_directly(self):
        async def _run_activate():
            async with async_session_factory() as session:
                return await activate_profile(
                    session, "AGGRESSIVE", changed_by="tester", reason="test"
                )

        assert _run(_run_activate()).code == "AGGRESSIVE"

    def test_activate_extreme_risk_succeeds_directly(self):
        async def _run_activate():
            async with async_session_factory() as session:
                return await activate_profile(
                    session, "HIGH_RISK", changed_by="tester", reason="test"
                )

        assert _run(_run_activate()).code == "HIGH_RISK"


# ── Admin routes (HTML) ──────────────────────────────────────────────────────


class TestAdminTradeProfilesRoutes:
    def _login(self, client: TestClient) -> None:
        login = client.post(
            "/admin/login",
            data={"password": settings.admin_password},
            follow_redirects=False,
        )
        assert login.status_code == 303

    def test_requires_auth(self, client: TestClient):
        resp = client.get("/admin/trade-profiles")
        assert resp.status_code == 401

    def test_page_lists_builtin_profiles(self, client: TestClient):
        self._login(client)
        resp = client.get("/admin/trade-profiles")
        assert resp.status_code == 200
        for code in ("CONSERVATIVE", "NORMAL", "AGGRESSIVE", "HIGH_RISK"):
            assert code in resp.text

    def test_page_exposes_every_editable_profile_field(self, client: TestClient):
        self._login(client)
        resp = client.get("/admin/trade-profiles")

        assert resp.status_code == 200
        for field in EDITABLE_FIELDS:
            assert f'name="{field}"' in resp.text

    def test_newly_exposed_sizing_fields_update_from_html(self, client: TestClient):
        self._login(client)
        resp = client.post(
            "/admin/trade-profiles/NORMAL/update",
            data={
                "risk_per_trade_pct": "0.40",
                "max_account_data_age_seconds": "45",
                "block_buy_on_near_ask_wall": "true",
                "reason": "admin coverage test",
            },
            follow_redirects=False,
        )

        assert resp.status_code == 303

        async def _get_updated():
            async with async_session_factory() as session:
                return await get_profile(session, "NORMAL")

        profile = _run(_get_updated())
        assert profile.risk_per_trade_pct == Decimal("0.40")
        assert profile.max_account_data_age_seconds == Decimal("45")
        assert profile.block_buy_on_near_ask_wall is True

    def test_create_profile(self, client: TestClient):
        self._login(client)
        resp = client.post(
            "/admin/trade-profiles/create",
            data={
                "code": "CUSTOM",
                "name": "Custom Profile",
                "description": "Created from admin",
                "risk_level": "LOW",
                "max_orders_per_day": "3",
            },
            follow_redirects=False,
        )

        assert resp.status_code == 303

        async def _get_created():
            async with async_session_factory() as session:
                return await get_profile(session, "CUSTOM")

        profile = _run(_get_created())
        assert profile.name == "Custom Profile"
        assert profile.description == "Created from admin"
        assert profile.risk_level == "LOW"
        assert profile.max_orders_per_day == 3

    def test_activate_redirects_directly(self, client: TestClient):
        self._login(client)
        resp = client.post(
            "/admin/trade-profiles/AGGRESSIVE/activate",
            data={"reason": "test"},
            follow_redirects=False,
        )
        assert resp.status_code == 303


# ── Admin API (JSON) ─────────────────────────────────────────────────────────


class TestAdminTradeProfilesApi:
    def test_list_requires_auth(self, client: TestClient):
        resp = client.get("/api/admin/trade-profiles")
        assert resp.status_code == 401

    def test_list_returns_four_profiles(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        resp = client.get("/api/admin/trade-profiles", headers=auth_headers)
        assert resp.status_code == 200
        codes = {p["code"] for p in resp.json()}
        assert codes == {"CONSERVATIVE", "NORMAL", "AGGRESSIVE", "HIGH_RISK"}
        active = next(p for p in resp.json() if p["code"] == "NORMAL")
        assert active["isActive"] is True

    def test_activate_via_api_directly(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        resp = client.post(
            "/api/admin/trade-profiles/HIGH_RISK/activate",
            json={"reason": "test"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["code"] == "HIGH_RISK"


# ── Integration: bot config + RiskEngine ─────────────────────────────────────


class TestBotConfigIntegration:
    def test_active_profile_change_changes_config_hash(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        before = client.get("/api/gateway/config", headers=auth_headers).json()

        activate = client.post(
            "/api/admin/trade-profiles/AGGRESSIVE/activate",
            json={"reason": "test"},
            headers=auth_headers,
        )
        assert activate.status_code == 200

        after = client.get("/api/gateway/config", headers=auth_headers).json()
        assert after["configHash"] != before["configHash"]

        async def _effective_max_order_value():
            from app.services.effective_risk_config import (
                resolve_effective_risk_config,
            )

            async with async_session_factory() as session:
                limits = await resolve_effective_risk_config(session)
                return limits.max_order_value_tl

        assert Decimal(str(after["maxOrderValueTl"])) == _run(
            _effective_max_order_value()
        )
        assert (
            after["scanIntervalMinutes"]
            == BUILTIN_PROFILES["AGGRESSIVE"]["scan_interval_minutes"]
        )
        assert after["activeTradeProfile"]["code"] == "AGGRESSIVE"


class TestRiskEngineIntegration:
    def test_build_runtime_risk_config_reflects_active_profile(self):
        async def _run_flow():
            async with async_session_factory() as session:
                await activate_profile(
                    session, "AGGRESSIVE", changed_by="tester"
                )

            from app.services.admin_config import build_runtime_risk_config

            async with async_session_factory() as session:
                return await build_runtime_risk_config(session)

        cfg = _run(_run_flow())
        assert (
            cfg.min_confidence_for_buy
            == BUILTIN_PROFILES["AGGRESSIVE"]["min_confidence_for_buy"]
        )
        assert (
            cfg.max_position_value_per_symbol
            == BUILTIN_PROFILES["AGGRESSIVE"]["max_position_value_per_symbol"]
        )
        assert (
            cfg.require_alpha_trend_alignment
            == BUILTIN_PROFILES["AGGRESSIVE"]["require_alpha_trend_alignment"]
        )
        assert cfg.real_live_mode_allowed is False  # AGGRESSIVE.allow_real_live=False


# ── Safety invariants unaffected by profiles ─────────────────────────────────


class TestProfileIndependentSafety:
    def test_kill_switch_blocks_regardless_of_active_profile(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        client.post(
            "/api/admin/trade-profiles/AGGRESSIVE/activate",
            json={"reason": "test"},
            headers=auth_headers,
        )
        update = client.put(
            "/api/admin/config/killSwitchEnabled",
            json={"value": True, "reason": "test"},
            headers=auth_headers,
        )
        assert update.status_code == 200

        resp = client.post(
            "/api/signal/evaluate",
            headers=auth_headers,
            json={
                "requestId": "kill-switch-test",
                "symbol": "THYAO",
                "timeframe": "1h",
                "lastPrice": 100.0,
                "open": 99.0,
                "high": 101.0,
                "low": 98.0,
                "volume": 1000.0,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["action"] == "WAIT"
        assert "Kill switch" in resp.json()["reason"]

    def test_bot_allow_market_orders_still_rejected_under_any_profile(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        client.post(
            "/api/admin/trade-profiles/HIGH_RISK/activate",
            json={"reason": "test"},
            headers=auth_headers,
        )
        resp = client.put(
            "/api/admin/config/botAllowMarketOrders",
            json={"value": True, "reason": "test"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
