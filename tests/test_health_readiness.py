"""Readiness kapılarının v2 semantiği.

Bu üç kontrol de cutover sırasında sessizce bozulmuştu (legacy runtimeMode
karşılaştırması, negatif yaşın "taze" sayılması, eskimiş migration sabiti) ve
hiçbiri test kapsamında değildi. Testler kapıların fail-closed kalmasını korur.
"""

from __future__ import annotations

import json

import pytest

import app.routers.health as health_router
from app.db.init_db import drop_all, init_db

pytestmark = pytest.mark.asyncio


def _gateway_payload(**overrides):
    """v2 gateway /health yanıtının readiness'i ilgilendiren alanları."""
    payload = {
        "ok": True,
        "configStale": False,
        "configVersion": "v1",
        "configAgeSeconds": 1,
        "systemMode": "AUTO_TRADE",
        "accountType": "DEMO",
        "realAccountArmed": False,
        "accountVerificationAgeSeconds": 1,
        "positionSyncAgeSeconds": 1,
        "quoteAgeSeconds": {"THYAO": 1},
        "depthAgeSeconds": {"THYAO": 1},
        "callbackQueueDepth": 0,
        "callbackOutboxBacklog": 0,
    }
    payload.update(overrides)
    return payload


async def _ready_payload(monkeypatch, **overrides):
    await drop_all()
    await init_db()

    async def gateway_health():
        return _gateway_payload(**overrides)

    monkeypatch.setattr(health_router.gateway_client, "health", gateway_health)
    resp = await health_router.health_ready()
    return json.loads(resp.body)


async def test_fresh_demo_auto_trade_is_ready(monkeypatch):
    payload = await _ready_payload(monkeypatch)
    assert payload["status"] == "ready"
    assert payload["checks"]["marketData"]["ok"] is True
    assert payload["checks"]["demoAccount"]["ok"] is True


class TestQuoteFreshness:
    async def test_negative_quote_age_is_not_ready(self, monkeypatch):
        """Gelecekten gelen damga (saat kayması) sağlıklı sayılmamalı.

        Emir yolu bunu zaten reddediyor; readiness yeşil kalırsa emirler
        sessizce bloklanırken panel sorunu gizler.
        """
        payload = await _ready_payload(
            monkeypatch, quoteAgeSeconds={"THYAO": -9011.8}
        )
        assert payload["checks"]["marketData"]["ok"] is False
        assert payload["status"] == "not_ready"

    async def test_stale_quote_age_is_not_ready(self, monkeypatch):
        payload = await _ready_payload(monkeypatch, quoteAgeSeconds={"THYAO": 16})
        assert payload["checks"]["marketData"]["ok"] is False

    async def test_one_stale_symbol_among_fresh_keeps_global_feed_ready(
        self, monkeypatch
    ):
        """Readiness global akışı, order preflight sembolü ayrı doğrular."""
        payload = await _ready_payload(
            monkeypatch, quoteAgeSeconds={"THYAO": 1, "GARAN": 16}
        )
        market_data = payload["checks"]["marketData"]
        assert market_data["ok"] is True
        assert market_data["freshSymbols"] == ["THYAO"]
        assert market_data["staleSymbols"] == ["GARAN"]
        assert payload["status"] == "ready"

    async def test_one_fresh_symbol_among_missing_keeps_global_feed_ready(
        self, monkeypatch
    ):
        payload = await _ready_payload(
            monkeypatch,
            symbols=["THYAO", "GARAN"],
            quoteAgeSeconds={"THYAO": 1, "GARAN": None},
        )
        market_data = payload["checks"]["marketData"]
        assert market_data["ok"] is True
        assert market_data["freshSymbolCount"] == 1
        assert market_data["missingSymbols"] == ["GARAN"]

    async def test_missing_quote_timestamps_are_not_ready(self, monkeypatch):
        """Tüm damgalar null → tazelik kanıtlanamaz → hazır değil."""
        payload = await _ready_payload(monkeypatch, quoteAgeSeconds={"THYAO": None})
        assert payload["checks"]["marketData"]["ok"] is False

    async def test_future_timestamp_symbol_is_reported(self, monkeypatch):
        payload = await _ready_payload(
            monkeypatch, quoteAgeSeconds={"THYAO": -9011.8}
        )
        assert payload["checks"]["marketData"]["futureTimestampSymbols"] == [
            "THYAO"
        ]

    async def test_negative_depth_age_is_not_ready(self, monkeypatch):
        payload = await _ready_payload(monkeypatch, depthAgeSeconds={"THYAO": -300})
        assert payload["checks"]["marketData"]["ok"] is False

    async def test_invalid_depth_age_is_not_treated_as_absent(self, monkeypatch):
        payload = await _ready_payload(
            monkeypatch, depthAgeSeconds={"THYAO": "invalid"}
        )
        assert payload["checks"]["marketData"]["ok"] is False
        assert payload["checks"]["marketData"][
            "depthEventFreshnessAvailable"
        ] is True

    async def test_absent_depth_timestamps_do_not_block(self, monkeypatch):
        """Desteklenen Matriks build'lerinde derinlik damgası hiç gelmiyor;
        bu sözleşme kısıtı readiness'i kalıcı kırmızı yapmamalı."""
        payload = await _ready_payload(monkeypatch, depthAgeSeconds={})
        assert payload["checks"]["marketData"]["ok"] is True


class TestAccountGate:
    async def test_observe_only_ignores_account_identity(self, monkeypatch):
        """Dispatch kapalıyken hesap kimliği readiness'i etkilemez."""
        payload = await _ready_payload(
            monkeypatch,
            systemMode="OBSERVE_ONLY",
            accountType="UNKNOWN",
            accountVerificationAgeSeconds=None,
        )
        assert payload["checks"]["demoAccount"]["ok"] is True

    async def test_auto_trade_with_unknown_account_is_not_ready(self, monkeypatch):
        payload = await _ready_payload(monkeypatch, accountType="UNKNOWN")
        assert payload["checks"]["demoAccount"]["ok"] is False
        assert payload["status"] == "not_ready"

    async def test_auto_trade_with_stale_verification_is_not_ready(self, monkeypatch):
        payload = await _ready_payload(monkeypatch, accountVerificationAgeSeconds=6)
        assert payload["checks"]["demoAccount"]["ok"] is False

    async def test_auto_trade_with_missing_verification_is_not_ready(self, monkeypatch):
        payload = await _ready_payload(monkeypatch, accountVerificationAgeSeconds=None)
        assert payload["checks"]["demoAccount"]["ok"] is False

    async def test_unarmed_real_account_is_not_ready(self, monkeypatch):
        """REAL hesap arming'siz AUTO_TRADE'de hazır sayılmamalı."""
        payload = await _ready_payload(
            monkeypatch, accountType="REAL", realAccountArmed=False
        )
        assert payload["checks"]["demoAccount"]["ok"] is False

    async def test_armed_real_account_is_ready(self, monkeypatch):
        payload = await _ready_payload(
            monkeypatch, accountType="REAL", realAccountArmed=True
        )
        assert payload["checks"]["demoAccount"]["ok"] is True

    async def test_legacy_runtime_mode_no_longer_grants_readiness(self, monkeypatch):
        """Cutover regresyonunun bekçisi: eski kapı runtimeMode != "DEMO_LIVE"
        karşılaştırmasına dayanıyordu ve v2 gateway'inde her zaman doğruya
        düşüp kontrolü öldürüyordu. systemMode artık tek yetkili alan."""
        payload = await _ready_payload(
            monkeypatch,
            runtimeMode="AUTO_TRADE",
            accountType="UNKNOWN",
        )
        assert payload["checks"]["demoAccount"]["ok"] is False


async def test_expected_migration_matches_alembic_head():
    """EXPECTED_MIGRATION elle güncelleniyor; head ilerleyince eskiyor ve
    üretimde readiness'i haksız yere kırmızı yapar."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert list(script.get_heads()) == [health_router.EXPECTED_MIGRATION]
