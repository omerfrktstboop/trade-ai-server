"""Tests for the Matriks gateway client (app/services/matriks_gateway.py).

Fake gateway stub'ına (tests/fake_gateway.py) karşı koşar — gerçek Matriks
gerektirmez.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    MatriksGatewayClient,
)
from tests.fake_gateway import FakeGateway


def make_client(fake: FakeGateway, token: str | None = None) -> MatriksGatewayClient:
    return MatriksGatewayClient(
        base_url="http://fake-gateway",
        token=fake.token if token is None else token,
        transport=fake.transport,
    )


def test_gateway_bars_contract_uses_period_series_and_reliable_ohlcv():
    source = (
        Path(__file__).resolve().parents[1] / "matriks" / "TradeAiGateway.cs"
    ).read_text(encoding="utf-8")

    assert "string seriesKey = BuildSeriesKey(symbol, actualPeriod);" in source
    assert "_ohlcvHistoryBySeries.TryGetValue(seriesKey" in source
    assert "actualBarPeriod = actualPeriod" in source
    assert "volume = ToDouble(point.Volume)" in source
    assert "reliable = point.Reliable" in source
    assert "closed = point.Closed" in source


# ═══════════════════════════════════════════════════════════════════════════════
# Health
# ═══════════════════════════════════════════════════════════════════════════════


class TestHealth:
    async def test_health_returns_gateway_state(self):
        fake = FakeGateway()
        client = make_client(fake)

        health = await client.health()

        assert health["ok"] is True
        assert health["server"] == "TradeAiGateway"
        assert health["phase"] == "read-only"
        assert health["symbols"] == ["THYAO", "AKBNK"]
        assert health["subscriptionsInitialized"] is True
        await client.close()

    async def test_is_available_true_when_healthy(self):
        fake = FakeGateway()
        client = make_client(fake)

        assert await client.is_available() is True
        await client.close()

    async def test_is_available_false_on_wrong_token(self):
        fake = FakeGateway()
        client = make_client(fake, token="wrong-token")

        assert await client.is_available() is False
        await client.close()


# ═══════════════════════════════════════════════════════════════════════════════
class TestMarketRankingCapabilities:
    async def test_ranking_capabilities_report_scoped_fallback(self):
        fake = FakeGateway()
        client = make_client(fake)

        capabilities = await client.get_market_ranking_capabilities()

        assert capabilities["nativeMarketWide"] is False
        assert capabilities["universe"] == "CONFIGURED_SUBSCRIBED_EQUITY_ONLY"
        assert capabilities["weeklyGainers"]["source"] == "SUBSCRIBED_UNIVERSE_FALLBACK"
        assert capabilities["weeklyGainers"]["referencePeriod"] == "SEVEN_SESSIONS"
        assert capabilities["weeklyGainers"]["calendarWeekEquivalent"] is False
        assert capabilities["relativeVolumeLeaders"]["available"] is False
        await client.close()

    async def test_missing_contract_is_fail_closed(self):
        fake = FakeGateway()
        fake.capabilities_payload = {"ok": True, "capabilities": {}}
        client = make_client(fake)

        capabilities = await client.get_market_ranking_capabilities()

        assert capabilities["nativeMarketWide"] is False
        assert capabilities["source"] == "UNAVAILABLE"
        assert capabilities["weeklyGainers"]["available"] is False
        assert capabilities["turnoverLeaders"]["available"] is False
        assert capabilities["relativeVolumeLeaders"]["available"] is False
        await client.close()
# Auth
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuth:
    async def test_wrong_token_raises_gateway_error_401(self):
        fake = FakeGateway()
        client = make_client(fake, token="wrong-token")

        with pytest.raises(GatewayError) as exc_info:
            await client.health()
        assert exc_info.value.status_code == 401
        await client.close()

    async def test_auth_header_sent_as_bearer(self):
        fake = FakeGateway()
        client = make_client(fake)

        await client.health()

        assert fake.request_log[-1].headers["Authorization"] == f"Bearer {fake.token}"
        await client.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Snapshot
# ═══════════════════════════════════════════════════════════════════════════════


class TestSnapshot:
    async def test_snapshot_returns_full_payload(self):
        fake = FakeGateway()
        client = make_client(fake)

        snapshot = await client.get_snapshot("THYAO")

        assert snapshot["ok"] is True
        assert snapshot["symbol"] == "THYAO"
        payload = snapshot["payload"]
        assert payload["lastPrice"] == 71.5
        assert payload["rsi"] == 55.0
        assert payload["technicalFeatures"]["schemaVersion"] == "technical-features-v1"
        assert payload["technicalFeatures"]["alphaTrendSignal"] == "NEUTRAL"
        await client.close()

    async def test_snapshot_symbol_is_normalized_to_uppercase(self):
        fake = FakeGateway()
        client = make_client(fake)

        snapshot = await client.get_snapshot("  thyao ")

        assert snapshot["symbol"] == "THYAO"
        await client.close()

    async def test_snapshot_unknown_symbol_raises_gateway_error(self):
        fake = FakeGateway()
        client = make_client(fake)

        with pytest.raises(GatewayError) as exc_info:
            await client.get_snapshot("NOPE")
        assert exc_info.value.status_code == 400
        await client.close()

    async def test_snapshot_overrides_flow_through(self):
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {"lastPrice": 99.9, "quoteReliable": False}
        client = make_client(fake)

        snapshot = await client.get_snapshot("THYAO")

        assert snapshot["payload"]["lastPrice"] == 99.9
        assert snapshot["payload"]["quoteReliable"] is False
        await client.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Positions
# ═══════════════════════════════════════════════════════════════════════════════


class TestPositions:
    async def test_positions_returns_entries(self):
        fake = FakeGateway()
        client = make_client(fake)

        result = await client.get_positions()

        assert result["ok"] is True
        assert result["positionsLoaded"] is True
        symbols = {p["symbol"] for p in result["positions"]}
        assert symbols == {"THYAO", "AKBNK"}
        thyao = next(p for p in result["positions"] if p["symbol"] == "THYAO")
        assert thyao["lockedLongTermQty"] == 100.0
        await client.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Unreachable gateway / retry
# ═══════════════════════════════════════════════════════════════════════════════


class TestUnavailable:
    async def test_connect_error_raises_gateway_unavailable(self):
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = MatriksGatewayClient(
            base_url="http://fake-gateway",
            token="x",
            transport=httpx.MockTransport(refuse),
        )

        with pytest.raises(GatewayUnavailable):
            await client.health()
        await client.close()

    async def test_transport_error_retried_once_then_succeeds(self):
        fake = FakeGateway()
        calls = {"count": 0}

        def flaky(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            if calls["count"] == 1:
                raise httpx.ConnectTimeout("timeout", request=request)
            return fake._handle(request)

        client = MatriksGatewayClient(
            base_url="http://fake-gateway",
            token=fake.token,
            transport=httpx.MockTransport(flaky),
        )

        health = await client.health()

        assert health["ok"] is True
        assert calls["count"] == 2
        await client.close()

    async def test_is_available_false_when_unreachable(self):
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = MatriksGatewayClient(
            base_url="http://fake-gateway",
            token="x",
            transport=httpx.MockTransport(refuse),
        )

        assert await client.is_available() is False
        await client.close()


# ═══════════════════════════════════════════════════════════════════════════════
# send_order (Phase 2)
# ═══════════════════════════════════════════════════════════════════════════════


class TestSendOrder:
    async def test_accepted_order_returns_sent_pending(self):
        fake = FakeGateway()
        client = make_client(fake)

        outcome = await client.send_order(
            request_id="THYAO-20260709-120000-scan",
            symbol="thyao",
            side="buy",
            qty=1.0,
            limit_price=71.5,
            mode="demo_live",
        )

        assert outcome["accepted"] is True
        assert outcome["status"] == "SENT_PENDING"
        sent = fake.orders[0]
        assert sent["symbol"] == "THYAO"
        assert sent["side"] == "BUY"
        assert sent["mode"] == "DEMO_LIVE"
        assert sent["limitPrice"] == 71.5
        await client.close()

    async def test_rejected_order_returns_accepted_false(self):
        fake = FakeGateway()
        fake.order_rejection = "MaxOrdersPerDay reached: 1"
        client = make_client(fake)

        outcome = await client.send_order(
            request_id="x-1",
            symbol="THYAO",
            side="SELL",
            qty=1.0,
            limit_price=71.5,
            mode="DEMO_LIVE",
        )

        assert outcome["accepted"] is False
        assert outcome["status"] == "REJECTED"
        assert "MaxOrdersPerDay" in outcome["reason"]
        await client.close()

    async def test_transport_error_not_retried(self):
        calls = {"count": 0}

        def refuse(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            raise httpx.ConnectError("connection refused", request=request)

        client = MatriksGatewayClient(
            base_url="http://fake-gateway",
            token="x",
            transport=httpx.MockTransport(refuse),
        )

        with pytest.raises(GatewayUnavailable):
            await client.send_order(
                request_id="x-1",
                symbol="THYAO",
                side="BUY",
                qty=1.0,
                limit_price=71.5,
                mode="DEMO_LIVE",
            )

        # Emirde retry YOK — çift emir riski
        assert calls["count"] == 1
        await client.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Genişletilmiş veri yüzeyi (read-only passthrough client metodları)
# ═══════════════════════════════════════════════════════════════════════════════


class TestDataSurface:
    def _recording_client(self, seen: dict):
        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json={"ok": True, "available": True})

        return MatriksGatewayClient(
            base_url="http://fake-gateway",
            token="x",
            transport=httpx.MockTransport(handler),
        )

    async def test_get_market_data_hits_field_endpoint(self):
        seen: dict = {}
        client = self._recording_client(seen)

        await client.get_market_data("thyao", "Bid")

        assert seen["path"] == "/marketdata"
        assert seen["params"] == {"symbol": "THYAO", "field": "Bid"}
        await client.close()

    async def test_get_market_data_all(self):
        seen: dict = {}
        client = self._recording_client(seen)
        await client.get_market_data_all("akbnk")
        assert seen["path"] == "/marketdata/all"
        assert seen["params"]["symbol"] == "AKBNK"
        await client.close()

    async def test_kap_endpoints(self):
        seen: dict = {}
        client = self._recording_client(seen)
        await client.get_kap("thyao", limit=99)
        assert seen["path"] == "/kap"
        assert seen["params"] == {"symbol": "THYAO", "limit": "99"}
        await client.get_kap_risk("thyao", lookback_hours=48)
        assert seen["path"] == "/kap/risk"
        assert seen["params"] == {"symbol": "THYAO", "lookbackHours": "48"}
        await client.close()

    async def test_symbol_session_pricestep_bars(self):
        seen: dict = {}
        client = self._recording_client(seen)

        await client.get_symbol_info("thyao")
        assert seen["path"] == "/symbol"
        await client.get_session_times("thyao")
        assert seen["path"] == "/session"
        await client.get_price_step("thyao", 71.5)
        assert seen["path"] == "/pricestep"
        assert seen["params"]["price"] == "71.5"
        await client.get_bars("thyao", count=10)
        assert seen["path"] == "/bars"
        assert seen["params"]["count"] == "10"
        await client.close()

    async def test_account_realpositions_overall_catalog(self):
        seen: dict = {}
        client = self._recording_client(seen)

        await client.get_account()
        assert seen["path"] == "/account"
        await client.get_real_positions()
        assert seen["path"] == "/realpositions"
        await client.get_overall()
        assert seen["path"] == "/overall"
        await client.get_method_catalog()
        assert seen["path"] == "/capabilities/methods"
        await client.search_methods("kap")
        assert seen["path"] == "/methods/search"
        assert seen["params"]["keyword"] == "kap"
        await client.close()

    async def test_bars_count_clamped(self):
        seen: dict = {}
        client = self._recording_client(seen)
        await client.get_bars("thyao", count=9999)
        assert seen["params"]["count"] == "500"
        await client.close()


class TestOrderStateEndpoints:
    async def test_active_orders_are_returned(self):
        fake = FakeGateway()
        fake.order_states = [{"orderId": "O-1", "requestId": "R-1", "status": "FILLED"}]
        client = make_client(fake)
        result = await client.get_active_orders()
        assert result["orders"][0]["status"] == "FILLED"
        await client.close()

    async def test_cancel_order_is_single_request(self):
        fake = FakeGateway()
        client = make_client(fake)
        result = await client.cancel_order("O-1")
        assert result["status"] == "CANCEL_REQUESTED"
        assert fake.cancelled_order_ids == ["O-1"]
        await client.close()
