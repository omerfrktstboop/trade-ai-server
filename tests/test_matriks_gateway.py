"""Tests for the Matriks gateway client (app/services/matriks_gateway.py).

Fake gateway stub'ına (tests/fake_gateway.py) karşı koşar — gerçek Matriks
gerektirmez.
"""

from __future__ import annotations

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
