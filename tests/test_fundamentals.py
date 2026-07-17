"""Tests for admin-entered symbol fundamentals (symbol_fundamentals table,
fundamentals_service, admin routes, and AI payload wiring)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.main import app
from app.services.fundamentals_service import (
    get_fundamentals_context,
    list_fundamentals,
    upsert_fundamental,
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


async def _seed_thyao() -> None:
    async with async_session_factory() as session:
        await upsert_fundamental(
            session,
            "THYAO",
            period="2026/Q2",
            changed_by="test",
            fcf_growth_pct=12.5,
            debt_to_equity=0.8,
            net_margin_pct=14.2,
            net_margin_change_pt=1.3,
            revenue_growth_pct=22.0,
            notes="test notu",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Service: upsert / list / context
# ═══════════════════════════════════════════════════════════════════════════════


class TestFundamentalsService:
    def test_upsert_creates_then_updates_single_row(self):
        async def _run():
            async with async_session_factory() as session:
                await upsert_fundamental(
                    session,
                    "thyao",
                    period="2026/Q1",
                    changed_by="t",
                    fcf_growth_pct=5.0,
                )
                await upsert_fundamental(
                    session,
                    "THYAO",
                    period="2026/Q2",
                    changed_by="t2",
                    debt_to_equity=1.1,
                )
                rows = await list_fundamentals(session)
                return rows

        rows = asyncio.run(_run())
        assert len(rows) == 1
        row = rows[0]
        assert row.symbol == "THYAO"
        assert row.period == "2026/Q2"
        assert row.updated_by == "t2"
        # Full-row semantics: omitted fields are cleared on update.
        assert row.fcf_growth_pct is None
        assert row.debt_to_equity == 1.1

    def test_upsert_requires_period(self):
        async def _run():
            async with async_session_factory() as session:
                await upsert_fundamental(session, "THYAO", period="  ", changed_by="t")

        with pytest.raises(ValueError, match="period"):
            asyncio.run(_run())

    def test_upsert_rejects_unknown_field(self):
        async def _run():
            async with async_session_factory() as session:
                await upsert_fundamental(
                    session,
                    "THYAO",
                    period="2026/Q2",
                    changed_by="t",
                    pe_ratio=10.0,
                )

        with pytest.raises(ValueError, match="Unknown fundamentals field"):
            asyncio.run(_run())

    def test_context_contains_only_symbols_with_rows(self):
        asyncio.run(_seed_thyao())

        context = asyncio.run(get_fundamentals_context(["THYAO", "AKBNK"]))

        assert "THYAO" in context
        assert "AKBNK" not in context  # no placeholder noise
        assert context["THYAO"]["period"] == "2026/Q2"
        assert context["THYAO"]["fcfGrowthPct"] == 12.5
        assert context["THYAO"]["debtToEquity"] == 0.8
        assert context["THYAO"]["netMarginChangePt"] == 1.3
        assert context["THYAO"]["updatedAt"] is not None

    def test_empty_symbols_returns_empty(self):
        assert asyncio.run(get_fundamentals_context([])) == {}

    def test_db_error_degrades_to_empty_context(self, monkeypatch):
        def _boom():
            raise RuntimeError("db unavailable")

        monkeypatch.setattr(
            "app.services.fundamentals_service.async_session_factory", _boom
        )

        assert asyncio.run(get_fundamentals_context(["THYAO"])) == {}


# ═══════════════════════════════════════════════════════════════════════════════
# Payload wiring
# ═══════════════════════════════════════════════════════════════════════════════


class TestFundamentalsInPayload:
    def _req(self):
        from app.models.signal import SignalRequest

        return SignalRequest(
            requestId="f-1",
            symbol="THYAO",
            timeframe="1h",
            lastPrice=100.0,
            open=99.0,
            high=102.0,
            low=98.0,
            volume=1000.0,
        )

    def test_build_payload_includes_fundamentals_context(self):
        from app.services.evaluator import build_payload as _build_payload

        ctx = {"THYAO": {"period": "2026/Q2", "fcfGrowthPct": 12.5}}
        payload = _build_payload(self._req(), fundamentals_context=ctx)

        assert payload["fundamentalsContext"] == ctx

    def test_build_payload_omits_empty_fundamentals_context(self):
        from app.services.evaluator import build_payload as _build_payload

        payload = _build_payload(self._req(), fundamentals_context={})
        assert "fundamentalsContext" not in payload

    def test_evaluate_endpoint_persists_fundamentals_in_raw_request(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        """End-to-end: entered fundamentals reach the persisted AI payload."""
        asyncio.run(_seed_thyao())

        resp = client.post(
            "/api/signal/evaluate",
            headers=auth_headers,
            json={
                "requestId": "fund-e2e-1",
                "symbol": "THYAO",
                "timeframe": "1h",
                "mode": "PAPER",
                "lastPrice": 100.0,
                "open": 99.0,
                "high": 101.0,
                "low": 98.0,
                "volume": 1000.0,
            },
        )
        assert resp.status_code == 200

        async def _load_raw_request():
            from sqlalchemy import select
            from app.models.db import AiDecision

            async with async_session_factory() as session:
                stmt = select(AiDecision).where(AiDecision.request_id == "fund-e2e-1")
                row = (await session.execute(stmt)).scalar_one_or_none()
                return row.raw_request if row else None

        raw_request = asyncio.run(_load_raw_request())
        assert raw_request is not None
        assert raw_request["fundamentalsContext"]["THYAO"]["fcfGrowthPct"] == 12.5

    def test_compact_prompt_excludes_audit_only_fundamentals(self):
        from app.core.prompts import get_trading_system_prompt

        prompt = get_trading_system_prompt()
        assert "fundamentalsContext" not in prompt
        assert "fcfGrowthPct" not in prompt
        assert "debtToEquity" not in prompt


# ═══════════════════════════════════════════════════════════════════════════════
# Admin HTML + API routes
# ═══════════════════════════════════════════════════════════════════════════════


class TestAdminFundamentalsRoutes:
    def _login(self, client: TestClient) -> None:
        login = client.post(
            "/admin/login",
            data={"password": settings.admin_password},
            follow_redirects=False,
        )
        assert login.status_code == 303

    def test_page_requires_auth(self, client: TestClient):
        assert client.get("/admin/fundamentals").status_code == 401

    def test_page_lists_watchlist_symbols(self, client: TestClient):
        self._login(client)
        resp = client.get("/admin/fundamentals")
        assert resp.status_code == 200
        assert "THYAO" in resp.text
        assert "fundamentalsContext" in resp.text  # info callout

    def test_html_upsert_roundtrip(self, client: TestClient):
        self._login(client)
        resp = client.post(
            "/admin/fundamentals/THYAO",
            data={
                "period": "2026/Q2",
                "fcf_growth_pct": "12.5",
                "debt_to_equity": "0.8",
                "net_margin_pct": "",
                "net_margin_change_pt": "",
                "revenue_growth_pct": "",
                "notes": "form notu",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        page = client.get("/admin/fundamentals")
        assert "2026/Q2" in page.text
        assert "12.5" in page.text
        assert "form notu" in page.text

    def test_html_upsert_missing_period_shows_error(self, client: TestClient):
        self._login(client)
        resp = client.post(
            "/admin/fundamentals/THYAO",
            data={"period": ""},
        )
        assert "period is required" in resp.text

    def test_html_delete_removes_row(self, client: TestClient):
        asyncio.run(_seed_thyao())
        self._login(client)

        resp = client.post("/admin/fundamentals/THYAO/delete", follow_redirects=False)
        assert resp.status_code == 303

        assert asyncio.run(get_fundamentals_context(["THYAO"])) == {}

    def test_api_list_and_upsert(
        self, client: TestClient, auth_headers: dict[str, str]
    ):
        resp = client.put(
            "/api/admin/fundamentals/thyao",
            headers=auth_headers,
            json={"period": "2026/Q2", "fcfGrowthPct": 9.9, "notes": "api notu"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["symbol"] == "THYAO"
        assert body["fcfGrowthPct"] == 9.9

        listing = client.get("/api/admin/fundamentals", headers=auth_headers)
        assert listing.status_code == 200
        assert [row["symbol"] for row in listing.json()] == ["THYAO"]

    def test_api_delete(self, client: TestClient, auth_headers: dict[str, str]):
        asyncio.run(_seed_thyao())

        resp = client.delete("/api/admin/fundamentals/THYAO", headers=auth_headers)
        assert resp.status_code == 200

        missing = client.delete("/api/admin/fundamentals/THYAO", headers=auth_headers)
        assert missing.status_code == 404
