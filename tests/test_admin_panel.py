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
from app.models.db import (
    AiDecision,
    BotPosition,
    ConfigAuditLog,
    MarketSnapshot,
    OrderLog,
    RiskDecision,
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
        # maxDailyTradeCount etc. moved to Trade Profiles (see test_trade_profiles.py);
        # botHttpTimeoutSeconds remains a standalone, admin-config-driven key.
        resp = client.put(
            "/api/admin/config/botHttpTimeoutSeconds",
            headers=auth_headers,
            json={"value": 30, "reason": "raise test limit"},
        )

        assert resp.status_code == 200
        assert resp.json()["value"] == "30"

        async def _load_audit() -> ConfigAuditLog | None:
            async with async_session_factory() as session:
                stmt = select(ConfigAuditLog).where(
                    ConfigAuditLog.key == "botHttpTimeoutSeconds"
                )
                return (await session.execute(stmt)).scalar_one_or_none()

        audit = asyncio.run(_load_audit())
        assert audit is not None
        assert audit.old_value == "15"
        assert audit.new_value == "30"
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

    def test_config_page_no_longer_shows_profile_shadowed_keys(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        """These 13 keys now come from the active Trade Profile — editing
        them here used to silently no-op. They must not appear as editable
        rows on /admin/config anymore."""
        resp = client.get("/admin/config", headers=auth_headers)

        assert resp.status_code == 200
        for removed_key in (
            "maxPositionValuePerSymbol",
            "maxDailyTradeCount",
            "minConfidenceForBuy",
            "minConfidenceForSell",
            "allowSellLongTerm",
            "botMaxOrderValueTl",
            "botMaxQtyPerOrder",
            "botMaxOrdersPerDay",
            "botMaxOrdersPerSymbolPerDay",
            "botScanIntervalMinutes",
            "botMaxFetchLoopPerSession",
            "botOrderTimeInForce",
            "botIndicatorPeriod",
        ):
            assert removed_key not in resp.text
        # Link to where these moved
        assert "/admin/trade-profiles" in resp.text


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

    def _seed_position(self, symbol: str, qty: float) -> None:
        """Seed bot_positions directly.

        The old POST /api/bot/positions/sync endpoint is gone — the scanner
        now pulls positions from the Matriks gateway (app/services/position_sync.py).
        """

        async def _run():
            async with async_session_factory() as session:
                session.add(BotPosition(symbol=symbol, qty=qty))
                await session.commit()

        asyncio.run(_run())

    def test_position_outside_allowed_symbols_shows_add_button(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        self._seed_position("ASELS", 10.0)
        self._login(client)

        resp = client.get("/admin/positions")
        assert resp.status_code == 200
        assert "İzleme listesinde değil" in resp.text
        assert "add-to-watchlist" in resp.text

    def test_add_to_watchlist_updates_allowed_symbols(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        self._seed_position("ASELS", 10.0)
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


class TestAdminDashboard:
    def test_dashboard_shows_active_trade_profile(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        resp = client.get("/admin", headers=auth_headers)

        assert resp.status_code == 200
        assert "Aktif Trade Profile" in resp.text
        assert "NORMAL" in resp.text


class TestLogsListBugFixes:
    """Regression coverage for two field-name mismatches that made the
    AI Decisions table's Confidence/Model columns and the Config Audit
    Logs table's Key column always render blank."""

    async def _seed_ai_decision(self, request_id: str) -> None:
        async with async_session_factory() as session:
            session.add(AiDecision(
                request_id=request_id, symbol="THYAO", provider="deepseek",
                raw_request={"symbol": "THYAO"}, raw_response={"action": "BUY"},
                action="BUY", confidence=91.5, qty=100.0, reason="bullish",
                model="deepseek-chat",
            ))
            await session.commit()

    async def _seed_audit_log(self) -> None:
        async with async_session_factory() as session:
            session.add(ConfigAuditLog(
                key="killSwitchEnabled", old_value="false", new_value="true",
                changed_by="admin", reason="test audit key rendering",
            ))
            await session.commit()

    def test_ai_decisions_table_shows_confidence_and_model(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        asyncio.run(self._seed_ai_decision("req-logs-list-1"))

        resp = client.get("/admin/logs", headers=auth_headers)

        assert resp.status_code == 200
        assert "91.5" in resp.text
        assert "deepseek-chat" in resp.text

    def test_audit_log_table_shows_key(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        asyncio.run(self._seed_audit_log())

        resp = client.get("/admin/logs", headers=auth_headers)

        assert resp.status_code == 200
        assert "killSwitchEnabled" in resp.text
        assert "test audit key rendering" in resp.text


class TestLogDeletion:
    async def _seed(self, rows: list) -> list[int]:
        async with async_session_factory() as session:
            session.add_all(rows)
            await session.commit()
            return [row.id for row in rows]

    def _ai_decisions(self, n: int) -> list[AiDecision]:
        return [
            AiDecision(
                request_id=f"del-ai-{i}", symbol="THYAO", provider="deepseek",
                raw_request={"i": i}, raw_response={"action": "BUY"},
                action="BUY", confidence=50.0, qty=10.0, reason="test",
                model="test-model",
            )
            for i in range(n)
        ]

    def _risk_decisions(self, n: int) -> list[RiskDecision]:
        return [
            RiskDecision(
                request_id=f"del-risk-{i}", symbol="THYAO", action="BUY",
                confidence=50.0, risk_score=10.0, allow_order=True,
                reason="test", qty=10.0, order_type="LIMIT", mode="PAPER",
            )
            for i in range(n)
        ]

    def _order_logs(self, n: int) -> list[OrderLog]:
        return [
            OrderLog(
                request_id=f"del-order-{i}", symbol="THYAO", action="BUY",
                qty=10.0, price=100.0, status="FILLED", mode="PAPER",
            )
            for i in range(n)
        ]

    def _audit_logs(self, n: int) -> list[ConfigAuditLog]:
        return [
            ConfigAuditLog(
                key="killSwitchEnabled", old_value="false", new_value="true",
                changed_by="admin", reason=f"test-{i}",
            )
            for i in range(n)
        ]

    def test_delete_all_requires_auth(self, client: TestClient):
        resp = client.post("/admin/logs/ai-decisions/delete-all")
        assert resp.status_code == 401

    def test_delete_selected_requires_auth(self, client: TestClient):
        resp = client.post("/admin/logs/ai-decisions/delete-selected")
        assert resp.status_code == 401

    def test_unknown_table_404s(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        resp = client.post(
            "/admin/logs/not-a-real-table/delete-all",
            headers=auth_headers,
            data={"reason": "test", "confirmation": "CONFIRM"},
        )
        assert resp.status_code == 404

    def test_delete_all_without_confirmation_leaves_rows(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        asyncio.run(self._seed(self._ai_decisions(3)))

        resp = client.post(
            "/admin/logs/ai-decisions/delete-all",
            headers=auth_headers,
            data={"reason": "test"},
        )
        assert "requires confirmation" in resp.text

        async def _count() -> int:
            async with async_session_factory() as session:
                return len((await session.execute(select(AiDecision))).scalars().all())

        assert asyncio.run(_count()) == 3

    def test_delete_all_with_confirmation_wipes_table(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        asyncio.run(self._seed(self._ai_decisions(3)))

        resp = client.post(
            "/admin/logs/ai-decisions/delete-all",
            headers=auth_headers,
            data={"reason": "cleanup", "confirmation": "CONFIRM"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        async def _count() -> int:
            async with async_session_factory() as session:
                return len((await session.execute(select(AiDecision))).scalars().all())

        assert asyncio.run(_count()) == 0

    def test_delete_selected_without_confirmation_leaves_rows(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        ids = asyncio.run(self._seed(self._risk_decisions(2)))

        resp = client.post(
            "/admin/logs/risk-decisions/delete-selected",
            headers=auth_headers,
            data={"reason": "test", "ids": [str(ids[0])]},
        )
        assert "requires confirmation" in resp.text

        async def _count() -> int:
            async with async_session_factory() as session:
                return len((await session.execute(select(RiskDecision))).scalars().all())

        assert asyncio.run(_count()) == 2

    def test_delete_selected_with_no_ids_shows_error(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        asyncio.run(self._seed(self._order_logs(1)))

        resp = client.post(
            "/admin/logs/order-logs/delete-selected",
            headers=auth_headers,
            data={"reason": "test", "confirmation": "CONFIRM"},
        )
        assert "Silinecek kayıt seçilmedi" in resp.text

        async def _count() -> int:
            async with async_session_factory() as session:
                return len((await session.execute(select(OrderLog))).scalars().all())

        assert asyncio.run(_count()) == 1

    def test_delete_selected_only_removes_chosen_rows(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        ids = asyncio.run(self._seed(self._order_logs(3)))

        resp = client.post(
            "/admin/logs/order-logs/delete-selected",
            headers=auth_headers,
            data={
                "reason": "remove first two",
                "confirmation": "CONFIRM",
                "ids": [str(ids[0]), str(ids[1])],
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        async def _remaining_ids() -> list[int]:
            async with async_session_factory() as session:
                rows = (await session.execute(select(OrderLog))).scalars().all()
                return [row.id for row in rows]

        remaining = asyncio.run(_remaining_ids())
        assert remaining == [ids[2]]

    def test_delete_all_audit_logs_allowed(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        """User explicitly chose to include Config Audit Logs in the
        deletable set (no protected/exempt table)."""
        asyncio.run(self._seed(self._audit_logs(2)))

        resp = client.post(
            "/admin/logs/audit-logs/delete-all",
            headers=auth_headers,
            data={"reason": "cleanup", "confirmation": "CONFIRM"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        async def _count() -> int:
            async with async_session_factory() as session:
                return len((await session.execute(select(ConfigAuditLog))).scalars().all())

        assert asyncio.run(_count()) == 0


class TestResearchRanking:
    """Unit tests for the pure ranking helpers behind /admin/research."""

    def test_rr_ratio_computed_from_entry_stop_target(self):
        from app.routers.admin import _research_rr_ratio

        # entry 100, stop 95 (risk 5), target 115 (reward 15) → 3.0x
        assert _research_rr_ratio(100.0, 95.0, 115.0) == 3.0

    def test_rr_ratio_none_when_any_leg_missing(self):
        from app.routers.admin import _research_rr_ratio

        assert _research_rr_ratio(None, 95.0, 115.0) is None
        assert _research_rr_ratio(100.0, None, 115.0) is None
        assert _research_rr_ratio(100.0, 95.0, None) is None

    def test_rr_ratio_none_when_stop_not_below_entry(self):
        from app.routers.admin import _research_rr_ratio

        assert _research_rr_ratio(100.0, 100.0, 115.0) is None
        assert _research_rr_ratio(100.0, 105.0, 115.0) is None

    def test_ranking_order_buy_by_rr_then_wait_then_sell(self):
        from types import SimpleNamespace
        from app.routers.admin import _research_rank_rows

        def _dec(symbol, action, confidence, entry_max=None, stop=None, target=None):
            return SimpleNamespace(
                symbol=symbol, action=action, confidence=confidence,
                risk_score=10.0, entry_min=entry_max, entry_max=entry_max,
                stop_loss=stop, target_price=target, reason="r",
                request_id=f"req-{symbol}", created_at=None,
            )

        ranked = _research_rank_rows([
            _dec("WAITHIGH", "WAIT", 90.0),
            _dec("BUYLOW", "BUY", 95.0, entry_max=100, stop=95, target=105),   # 1.0x
            _dec("SELLONE", "SELL", 99.0),
            _dec("BUYHIGH", "BUY", 60.0, entry_max=100, stop=95, target=120),  # 4.0x
            _dec("BUYNORR", "BUY", 99.0),  # BUY without price legs
            _dec("WAITLOW", "WAIT", 10.0),
        ])

        assert [r["symbol"] for r in ranked] == [
            "BUYHIGH", "BUYLOW", "BUYNORR", "WAITHIGH", "WAITLOW", "SELLONE",
        ]
        assert ranked[0]["rank"] == 1
        assert ranked[0]["rr"] == 4.0
        assert ranked[2]["rr"] is None


class TestResearchPage:
    async def _seed_decision(self, symbol, action, confidence, *, entry=None, stop=None, target=None):
        async with async_session_factory() as session:
            session.add(RiskDecision(
                request_id=f"research-{symbol}", symbol=symbol, action=action,
                confidence=confidence, risk_score=10.0, allow_order=action == "BUY",
                reason=f"{symbol} research seed", entry_min=entry, entry_max=entry,
                stop_loss=stop, target_price=target, qty=10.0,
                order_type="LIMIT", mode="PAPER",
            ))
            await session.commit()

    def test_requires_auth(self, client: TestClient):
        assert client.get("/admin/research").status_code == 401

    def test_empty_state_lists_watchlist_as_missing(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        resp = client.get("/admin/research", headers=auth_headers)
        assert resp.status_code == 200
        assert "hiç değerlendirme yok" in resp.text
        assert "THYAO" in resp.text  # in the missing-symbols section

    def test_ranked_page_orders_buy_above_wait(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        asyncio.run(self._seed_decision(
            "THYAO", "BUY", 80.0, entry=100.0, stop=95.0, target=120.0
        ))
        asyncio.run(self._seed_decision("AKBNK", "WAIT", 95.0))

        resp = client.get("/admin/research", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.text.index("THYAO") < resp.text.index("AKBNK")
        assert "4.00x" in resp.text  # R/R = (120-100)/(100-95)
        assert "/admin/logs/research-THYAO" in resp.text


class TestLocalTimeFilter:
    """DB timestamps are stored as UTC (func.now()) but the admin panel must
    display Europe/Istanbul local time — otherwise every row looks 3 hours
    behind the real wall clock."""

    def test_converts_naive_utc_to_istanbul(self):
        from datetime import datetime
        from app.routers.admin import _local_time

        # SQLite returns naive datetimes for DateTime(timezone=True) columns
        # written via func.now() — treated as UTC.
        naive_utc = datetime(2026, 7, 9, 8, 49, 53)
        assert _local_time(naive_utc) == "2026-07-09 11:49:53"

    def test_converts_aware_utc_to_istanbul(self):
        from datetime import UTC, datetime
        from app.routers.admin import _local_time

        aware_utc = datetime(2026, 7, 9, 8, 49, 53, tzinfo=UTC)
        assert _local_time(aware_utc) == "2026-07-09 11:49:53"

    def test_none_renders_dash(self):
        from app.routers.admin import _local_time

        assert _local_time(None) == "—"

    def test_registered_as_jinja_filter(self):
        from app.routers.admin import templates

        assert "local_time" in templates.env.filters
