"""Tests for the broker flow (smart-money / AKD) service and payload wiring."""

from __future__ import annotations

import json

from app.services.broker_flow_service import get_broker_flow_context
from app.services.matriks_gateway import GatewayUnavailable


# ── Fake gateway ────────────────────────────────────────────────────────────────


class FakeGateway:
    """Minimal stand-in exposing only ``get_institutions``."""

    def __init__(self, responses: dict[str, dict] | None = None, raise_exc=None):
        self._responses = responses or {}
        self._raise = raise_exc

    async def get_institutions(self, symbol: str, limit: int = 5) -> dict:
        if self._raise is not None:
            raise self._raise
        return self._responses.get(
            symbol.upper(), {"ok": True, "available": False, "symbol": symbol}
        )


class CountingGateway:
    def __init__(self):
        self.calls = 0

    async def get_institutions(
        self, symbol, limit=10, period="Daily", include_reported_orders=True
    ):
        self.calls += 1
        return _resp(symbol, [{"name": "Yatırım Fonları", "value": 100}], [])


async def test_akd_cache_avoids_second_gateway_call():
    gateway = CountingGateway()
    first = await get_broker_flow_context(["THYAO"], gateway=gateway)
    second = await get_broker_flow_context(["THYAO"], gateway=gateway)
    assert gateway.calls == 1
    assert first["THYAO"]["smartMoneyFlow"] == "STRONG_BUY"
    assert second["THYAO"]["dataAgeSeconds"] is None  # gateway supplied no asOf
    assert second["THYAO"]["retrievedAt"]


def _resp(symbol: str, buyers: list[dict], sellers: list[dict]) -> dict:
    return {
        "ok": True,
        "available": True,
        "symbol": symbol,
        "period": "DAILY",
        "buyers": buyers,
        "sellers": sellers,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Smart-money classification
# ═══════════════════════════════════════════════════════════════════════════════


class TestSmartMoneyClassification:
    async def test_funds_dominant_net_buy_is_strong_buy(self):
        gw = FakeGateway(
            {
                "THYAO": _resp(
                    "THYAO",
                    buyers=[
                        {"name": "Yatırım Fonları", "value": 600_000},
                        {"name": "Ziraat Yatırım", "value": 200_000},
                        {"name": "Retail Broker", "value": 200_000},
                    ],
                    sellers=[{"name": "Some Broker", "value": 300_000}],
                )
            }
        )

        result = await get_broker_flow_context(["THYAO"], gateway=gw)
        entry = result["THYAO"]

        assert entry["smartMoneyFlow"] == "STRONG_BUY"
        assert entry["brokerFlow"] == "BUY"
        assert entry["smartBuyRatio"] == 0.6  # 600k / 1M
        assert entry["netSmartLot"] == 600_000.0

    async def test_two_sided_fund_is_netted_out_to_neutral(self):
        """A fund big on BOTH sides must not fake a STRONG_BUY (wash-trade guard)."""
        gw = FakeGateway(
            {
                "AKBNK": _resp(
                    "AKBNK",
                    buyers=[
                        {"name": "Yatırım Fonları", "value": 500_000},
                        {"name": "Retail", "value": 500_000},
                    ],
                    sellers=[
                        {"name": "Yatırım Fonları", "value": 520_000},
                    ],
                )
            }
        )

        result = await get_broker_flow_context(["AKBNK"], gateway=gw)
        entry = result["AKBNK"]

        # buyRatio = 0.5 (≥0.40) but netSmartLot = 500k - 520k = -20k (not >0)
        assert entry["netSmartLot"] == -20_000.0
        assert entry["smartMoneyFlow"] != "STRONG_BUY"

    async def test_funds_dominant_net_sell_is_strong_sell(self):
        gw = FakeGateway(
            {
                "SISE": _resp(
                    "SISE",
                    buyers=[{"name": "Retail Broker", "value": 400_000}],
                    sellers=[
                        {"name": "Emeklilik Fonları", "value": 500_000},
                        {"name": "Retail", "value": 200_000},
                    ],
                )
            }
        )

        result = await get_broker_flow_context(["SISE"], gateway=gw)
        entry = result["SISE"]

        assert entry["smartMoneyFlow"] == "STRONG_SELL"
        assert entry["brokerFlow"] == "SELL"
        assert entry["netSmartLot"] < 0

    async def test_funds_present_but_not_dominant_is_neutral(self):
        gw = FakeGateway(
            {
                "GARAN": _resp(
                    "GARAN",
                    buyers=[
                        {"name": "Yatırım Fonları", "value": 100_000},
                        {"name": "Retail A", "value": 500_000},
                        {"name": "Retail B", "value": 400_000},
                    ],
                    sellers=[{"name": "Retail C", "value": 300_000}],
                )
            }
        )

        result = await get_broker_flow_context(["GARAN"], gateway=gw)
        entry = result["GARAN"]

        # smart buy ratio = 100k / 1M = 0.10 < 0.40
        assert entry["smartMoneyFlow"] == "NEUTRAL"

    async def test_citibank_foreign_counts_as_smart_money(self):
        gw = FakeGateway(
            {
                "TUPRS": _resp(
                    "TUPRS",
                    buyers=[
                        {"name": "Citibank Yabancı", "value": 700_000},
                        {"name": "Retail", "value": 300_000},
                    ],
                    sellers=[{"name": "Retail", "value": 100_000}],
                )
            }
        )

        result = await get_broker_flow_context(["TUPRS"], gateway=gw)
        assert result["TUPRS"]["smartMoneyFlow"] == "STRONG_BUY"


# ═══════════════════════════════════════════════════════════════════════════════
# Fail-closed behaviour
# ═══════════════════════════════════════════════════════════════════════════════


class TestFailClosed:
    async def test_gateway_unavailable_returns_unknown(self):
        gw = FakeGateway(raise_exc=GatewayUnavailable("matriks down"))

        result = await get_broker_flow_context(["THYAO"], gateway=gw)
        entry = result["THYAO"]

        assert entry["smartMoneyFlow"] == "UNKNOWN"
        assert entry["brokerFlow"] == "UNKNOWN"
        assert entry["netInstitutionalFlow"] is None

    async def test_unavailable_flag_returns_unknown(self):
        gw = FakeGateway({"THYAO": {"ok": True, "available": False, "symbol": "THYAO"}})

        result = await get_broker_flow_context(["THYAO"], gateway=gw)
        assert result["THYAO"]["smartMoneyFlow"] == "UNKNOWN"

    async def test_empty_symbol_list_returns_empty_dict(self):
        gw = FakeGateway()
        assert await get_broker_flow_context([], gateway=gw) == {}

    async def test_context_is_serializable(self):
        gw = FakeGateway(
            {
                "GARAN": _resp(
                    "GARAN",
                    buyers=[{"name": "Yatırım Fonları", "value": 500_000}],
                    sellers=[],
                )
            }
        )
        result = await get_broker_flow_context(["GARAN"], gateway=gw)
        assert json.dumps(result)  # does not raise


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: build_payload injects broker_flow_context
# ═══════════════════════════════════════════════════════════════════════════════


class TestBrokerFlowInPayload:
    def _req(self, symbol="THYAO"):
        from app.models.signal import SignalRequest

        return SignalRequest(
            requestId="test-1",
            symbol=symbol,
            timeframe="1h",
            lastPrice=100.0,
            open=99.0,
            high=102.0,
            low=98.0,
            volume=1000.0,
            rsi=50.0,
            ema20=98.0,
            ema50=95.0,
        )

    def test_build_payload_with_broker_flow_context(self):
        from app.services.evaluator import build_payload as _build_payload

        flow = {
            "THYAO": {
                "symbol": "THYAO",
                "smartMoneyFlow": "STRONG_BUY",
                "brokerFlow": "BUY",
                "netInstitutionalFlow": 1_250_000.0,
                "netSmartLot": 600_000.0,
                "topBrokers": [
                    {
                        "brokerName": "Yatırım Fonları",
                        "netFlow": 600_000.0,
                        "side": "BUY",
                    },
                ],
                "comment": "",
            }
        }

        payload = _build_payload(self._req(), broker_flow_context=flow)

        assert payload["brokerFlowContext"] == flow
        assert payload["brokerFlowContext"]["THYAO"]["smartMoneyFlow"] == "STRONG_BUY"

    def test_build_payload_without_broker_flow_context_excluded(self):
        from app.services.evaluator import build_payload as _build_payload

        assert "brokerFlowContext" not in _build_payload(self._req())

    def test_build_payload_with_none_broker_flow_context_excluded(self):
        from app.services.evaluator import build_payload as _build_payload

        assert "brokerFlowContext" not in _build_payload(
            self._req(), broker_flow_context=None
        )
