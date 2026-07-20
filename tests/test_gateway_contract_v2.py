"""v2 kontrat (Faz 3) testleri.

İki taraf tek atomik commit'te değişir:
- Python /api/gateway/config payload'ı contractVersion=2 gönderir.
- C# gateway contractVersion != 2'yi (alan eksikliği dahil) emir reddine
  çevirir ve CheckDispatchGates'i EK kapı olarak uygular (eski CheckModeGates
  yerinde kalır — geçiş dönemi çift fail-closed).

C# tarafı derlenemediği için (Matriks IQ içinde derlenir) buradaki güvenlik
invariant'ları kaynak üzerinde string-assert edilir — mevcut
test_gateway_order_contract.py deseniyle aynı.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db.init_db import drop_all, init_db
from app.main import app


def _source() -> str:
    return (Path(__file__).parents[1] / "matriks" / "TradeAiGateway.cs").read_text(
        encoding="utf-8"
    )


# ── Python tarafı: /api/gateway/config kontrat alanları ─────────────────────


@pytest.fixture
def _db():
    asyncio.run(drop_all())
    asyncio.run(init_db())
    yield


def test_gateway_config_payload_carries_contract_version_2(_db):
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    config = client.get("/api/gateway/config", headers=headers).json()
    assert config["ok"] is True
    assert config["contractVersion"] == 2
    # v2 cutover: eski mode/enableDemoOrders alanları kaldırıldı.
    assert "mode" not in config
    assert "enableDemoOrders" not in config
    assert config["killSwitchActive"] is False


def test_gateway_config_carries_v2_mode_and_arming_fields(_db):
    """Fix #1: C# CheckDispatchGates bu üç alanı okur; eksik gönderilirse
    gateway fail-closed OBSERVE_ONLY'ye düşer ve AUTO_TRADE asla çalışmaz."""
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    config = client.get("/api/gateway/config", headers=headers).json()
    assert config["systemMode"] == "OBSERVE_ONLY"  # default
    assert config["realAccountArmed"] is False
    assert config["armedAccountRef"] == ""


def test_gateway_config_reflects_auto_trade_when_set(_db):
    from app.db.session import async_session_factory
    from app.services.admin_config import set_admin_config_value

    async def _arm():
        async with async_session_factory() as session:
            await set_admin_config_value(
                session,
                "systemMode",
                "AUTO_TRADE",
                changed_by="test",
            )

    asyncio.run(_arm())
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    config = client.get("/api/gateway/config", headers=headers).json()
    assert config["systemMode"] == "AUTO_TRADE"


# ── C# tarafı: kaynak invariant'ları ────────────────────────────────────────


def test_gateway_rejects_contract_version_mismatch_fail_closed():
    source = _source()
    assert "private const int ExpectedContractVersion = 2;" in source
    handler = source.split("private async Task HandleOrderAsync", 1)[1]
    handler = handler.split("private decimal GetSellableQty", 1)[0]
    assert "_serverContractVersion != ExpectedContractVersion" in handler
    assert "contract version mismatch" in handler
    # Alan eksikliği de uyuşmazlıktır: default 0, asla 2 sayılmaz.
    assert 'cfg.Value<int?>("contractVersion") ?? 0' in source


def test_dispatch_gate_is_the_only_mode_gate():
    source = _source()
    # v2 cutover: eski CheckModeGates (PAPER/MANUAL/DEMO_LIVE/REAL_LIVE)
    # tamamen kaldırıldı; tek kapı CheckDispatchGates.
    assert "CheckModeGates(RuntimeMode)" not in source
    assert "private string CheckModeGates" not in source
    assert "rejection = CheckDispatchGates();" in source
    gates = source.split("private string CheckDispatchGates()", 1)[1]
    gates = gates.split("private static string NormalizeSystemMode", 1)[0]
    assert 'SystemMode != "AUTO_TRADE"' in gates
    assert "account verification failed or stale" in gates
    assert "account changed since last verification" in gates
    assert "RealAccountArmed" in gates
    assert "acct.AccountRef, ArmedAccountRef" in gates  # tekrar hash YOK


def test_unknown_system_mode_normalizes_to_observe_only():
    source = _source()
    normalize = source.split("private static string NormalizeSystemMode", 1)[1]
    normalize = normalize.split("private static string Sha256Hex", 1)[0]
    assert '"AUTO_TRADE" ? "AUTO_TRADE" : "OBSERVE_ONLY"' in normalize


def test_account_verification_populates_hashed_identity_fields():
    source = _source()
    verify = source.split("private void RefreshAccountVerification()", 1)[1]
    verify = verify.split("private sealed class AccountVerification", 1)[0]
    assert "GetTradeUser()" in verify
    assert "_lastVerifiedAccountRef = Sha256Hex(accountId);" in verify
    assert "_lastVerifiedSessionRef = Sha256Hex(accountId" in verify
    assert '_lastVerifiedAccountType = testAutoOrder ? "DEMO" : "REAL";' in verify
    # Hata yolunda tip UNKNOWN'a döner (fail-closed).
    assert '_lastVerifiedAccountType = "UNKNOWN";' in verify


def test_health_reports_contract_and_account_identity():
    source = _source()
    health = source.split("private async Task HandleHealthAsync", 1)[1]
    health = health.split("private async Task HandleSnapshotAsync", 1)[0]
    assert "gatewayContractVersion = ExpectedContractVersion" in health
    assert "systemMode = SystemMode" in health
    assert "accountRef =" in health
    assert "accountSessionRef =" in health
    assert "accountIdMasked =" in health
    assert "accountType = _lastVerifiedAccountType" in health
    # Ham hesap id'si health yanıtına asla yazılmaz.
    assert "accountId = _lastVerifiedAccountId" not in health


def test_account_endpoint_reports_hashed_identity_and_type():
    source = _source()
    handler = source.split("private async Task HandleAccountAsync", 1)[1]
    handler = handler.split("private async Task HandleRealPositionsAsync", 1)[0]
    assert "Sha256Hex(rawAccountId)" in handler
    assert "accountSessionRef" in handler
    assert "MaskAccountId(rawAccountId)" in handler
    assert '"DEMO" : "REAL"' in handler


def test_most_and_adx_native_probes_exist_and_feed_features():
    source = _source()
    assert "TryCreateNativeMostIndicator" in source
    assert "TryCreateNativeAdxIndicator" in source
    assert '"MOSTIndicator", "MOST"' in source
    assert '"ADXIndicator", "ADX"' in source
    features = source.split("private Dictionary<string, object> BuildTechnicalFeaturePayload", 1)[1]
    features = features.split("private VolatilitySnapshot ResolveVolatility", 1)[0]
    assert 'features["adx"]' in features
    assert 'features["most"]' in features
    assert 'features["mostSignal"]' in features
    assert '"LONG" : "SHORT"' in features


def test_hard_caps_untouched_by_v2_gates():
    """İlke #2: v2 kapıları hard cap reddi zincirinin YERİNE değil, YANINA
    eklendi — MaxQty/MaxOrderValue/günlük limit kontrolleri aynen duruyor."""
    source = _source()
    handler = source.split("private async Task HandleOrderAsync", 1)[1]
    handler = handler.split("private decimal GetSellableQty", 1)[0]
    assert "finalQty > MaxQtyPerOrder" in handler
    assert "orderValue > MaxOrderValueTl" in handler
    assert "GetTotalDailyOrderCount() >= MaxOrdersPerDay" in handler
    assert "GetDailyTradeCount(symbol) >= MaxOrdersPerSymbolPerDay" in handler
    assert "GetSellableQty(symbol) < qty" in handler

def test_snapshot_surfaces_real_depth_age_not_hardcoded_null():
    """order_preflight bağımsız bir kapı ve top-level depthAgeSeconds'i okur.

    Bu alan uzun süre sabit ``null`` yazılıyordu; derinlik artık gerçek bir
    same-session yaş (DepthEventTimestampAvailable) hesaplasa bile preflight'ın
    derinlik-tazelik kontrolü her emirde sessizce düşüyordu. C# emir kapısı
    (ValidateOrderMarketData) zaten depthAnalysis.DepthAgeSeconds'e güveniyor;
    snapshot payload'ı da aynı değeri yüzeye çıkarmalı ki iki kapı tutarlı olsun.
    """
    source = _source()
    # payload["depthAgeSeconds"] artık analiz değerinden türetiliyor, sabit değil.
    assert 'payload["depthAgeSeconds"] = null;' not in source
    assert "DepthEventTimestampAvailable" in source
    assert "depthAnalysis.DepthAgeSeconds < double.MaxValue" in source


def test_snapshot_bid_ask_fall_back_to_depth_best_when_quote_missing():
    """quote.Bid/Ask racy sıfır dönebilir; bu durumda emir fiyatlaması yapısal
    olarak doğrulanmış depth best bid/ask'e düşmeli ve payload bestAsk taşımalı
    (Python preflight'ın ask tarafı için tek dayanağı)."""
    source = _source()
    assert "if (bidPrice <= 0m && bestBid > 0m) bidPrice = bestBid;" in source
    assert (
        "if (askPrice <= 0m && depthAnalysis.BestAsk > 0m) askPrice = depthAnalysis.BestAsk;"
        in source
    )
    assert 'payload["bestAsk"] = ToDouble(depthAnalysis.BestAsk);' in source
