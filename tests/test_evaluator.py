"""Tests for the in-process evaluator (app/services/evaluator.py).

Fake gateway (tests/fake_gateway.py) + mock/stub AI provider ile koşar.
DB erişimi olmadığında evaluator'ın zarifçe düşmesi (statik config'e
fallback) tasarımın parçası — bu testler DB'siz de anlamlıdır.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.models.signal import SignalAction, SignalMode
from app.services.evaluator import evaluate_symbol
from app.services.matriks_gateway import GatewayError, MatriksGatewayClient
from tests.fake_gateway import FakeGateway


class StubProvider:
    """Kaydeden ve programlanabilir karar döndüren AI provider."""

    def __init__(self, raw: dict[str, Any] | None = None) -> None:
        self.raw = raw or {"action": "WAIT", "confidence": 0.0, "reason": "stub"}
        self.payloads: list[dict[str, Any]] = []

    async def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return self.raw


def make_gateway_client(fake: FakeGateway) -> MatriksGatewayClient:
    return MatriksGatewayClient(
        base_url="http://fake-gateway", token=fake.token, transport=fake.transport
    )


@pytest.fixture(autouse=True)
def _disable_preflight_gate(monkeypatch):
    """Bu suite provider'a ulaşan payload'ı doğrular; pre-flight token kapısı
    (NEUTRAL konsensüs + haber yok → LLM'siz WAIT) araya girmesin. Kapının
    kendi davranışı tests/test_decision_gate.py'de ayrıca test edilir."""

    def _no_gate(**_kwargs):
        return None

    monkeypatch.setattr("app.services.evaluator.preflight_wait_reason", _no_gate)


# ═══════════════════════════════════════════════════════════════════════════════
# Temel akış
# ═══════════════════════════════════════════════════════════════════════════════


class TestEvaluateSymbol:
    async def test_returns_final_decision_with_mock_wait(self):
        fake = FakeGateway()
        provider = StubProvider()

        result = await evaluate_symbol(
            "THYAO", gateway=make_gateway_client(fake), provider=provider
        )

        assert result is not None
        response = result.response
        assert response.symbol == "THYAO"
        assert response.action == SignalAction.WAIT
        assert response.allow_order is False

    async def test_request_id_format(self):
        fake = FakeGateway()
        provider = StubProvider()

        result = await evaluate_symbol(
            "THYAO", gateway=make_gateway_client(fake), provider=provider
        )

        assert result.response.request_id.startswith("THYAO-")
        assert result.response.request_id.endswith("-scan")

    async def test_ai_payload_contains_snapshot_fields_and_steps(self):
        fake = FakeGateway()
        provider = StubProvider()

        await evaluate_symbol(
            "THYAO", gateway=make_gateway_client(fake), provider=provider
        )

        assert len(provider.payloads) == 1
        payload = provider.payloads[0]
        assert payload["symbol"] == "THYAO"
        assert payload["lastPrice"] == 71.5
        assert payload["rsi"] == 55.0
        # agenticSteps eski ContextStep şemasında olmalı
        steps = payload["agenticSteps"]
        assert steps[0]["stepNo"] == 1
        assert steps[0]["symbol"] == "THYAO"
        assert steps[0]["dataType"] == "OHLCV"
        assert steps[0]["payload"]["lastPrice"] == 71.5

    async def test_no_usable_price_returns_none_without_ai_call(self):
        fake = FakeGateway()
        fake.snapshot_overrides["THYAO"] = {"lastPrice": 0.0}
        provider = StubProvider()

        result = await evaluate_symbol(
            "THYAO", gateway=make_gateway_client(fake), provider=provider
        )

        assert result is None
        assert provider.payloads == []

    async def test_unknown_symbol_raises_gateway_error(self):
        fake = FakeGateway()
        provider = StubProvider()

        with pytest.raises(GatewayError):
            await evaluate_symbol(
                "NOPE", gateway=make_gateway_client(fake), provider=provider
            )


# ═══════════════════════════════════════════════════════════════════════════════
# İlişkili sembol (RELATED_SYMBOLS) davranışı
# ═══════════════════════════════════════════════════════════════════════════════


class TestRelatedSymbols:
    async def test_related_symbol_snapshot_added_as_second_step(self):
        # ANELE → THYAO (evaluator.RELATED_SYMBOLS)
        fake = FakeGateway(symbols=["ANELE", "THYAO"])
        provider = StubProvider()

        await evaluate_symbol(
            "ANELE", gateway=make_gateway_client(fake), provider=provider
        )

        steps = provider.payloads[0]["agenticSteps"]
        assert len(steps) == 2
        assert steps[1]["symbol"] == "THYAO"
        assert steps[1]["dataType"] == "DEPTH"

    async def test_related_symbol_failure_does_not_block_decision(self):
        # THYAO gateway'in listesinde yok → ilişkili çağrı 400 alır ama karar üretilir
        fake = FakeGateway(symbols=["ANELE"])
        provider = StubProvider()

        result = await evaluate_symbol(
            "ANELE", gateway=make_gateway_client(fake), provider=provider
        )

        assert result is not None
        steps = provider.payloads[0]["agenticSteps"]
        assert len(steps) == 1

    async def test_symbol_without_related_mapping_single_step(self):
        fake = FakeGateway()
        provider = StubProvider()

        await evaluate_symbol(
            "AKBNK", gateway=make_gateway_client(fake), provider=provider
        )

        steps = provider.payloads[0]["agenticSteps"]
        assert len(steps) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Mode / güvenlik davranışı
# ═══════════════════════════════════════════════════════════════════════════════


class TestModeSafety:
    async def test_force_paper_clamps_mode(self):
        fake = FakeGateway()
        provider = StubProvider(
            raw={"action": "BUY", "confidence": 95.0, "qty": 10, "reason": "stub buy"}
        )

        result = await evaluate_symbol(
            "THYAO",
            gateway=make_gateway_client(fake),
            provider=provider,
            mode=SignalMode.REAL_LIVE,
            force_paper=True,
        )

        # PAPER'a sabitlendi: mode kanıtı + allowOrder asla true olamaz
        assert result.mode == SignalMode.PAPER
        assert result.response.allow_order is False

    async def test_paper_mode_buy_decision_never_allows_order(self):
        fake = FakeGateway()
        provider = StubProvider(
            raw={
                "action": "BUY",
                "confidence": 95.0,
                "qty": 10,
                "reason": "stub buy",
                "entryRange": {"min": 70.0, "max": 71.5},
            }
        )

        result = await evaluate_symbol(
            "THYAO",
            gateway=make_gateway_client(fake),
            provider=provider,
            mode=SignalMode.PAPER,
        )

        assert result.response.allow_order is False

    async def test_invalid_ai_action_falls_back_to_wait(self):
        fake = FakeGateway()
        provider = StubProvider(
            raw={"action": "YOLO", "confidence": 99.0, "reason": "garbage"}
        )

        result = await evaluate_symbol(
            "THYAO", gateway=make_gateway_client(fake), provider=provider
        )

        assert result.response.action == SignalAction.WAIT
