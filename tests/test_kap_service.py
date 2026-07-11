from datetime import UTC, datetime, timedelta

from app.services.kap_service import _is_active_risk, _published, classify_kap
import app.services.kap_service as kap_service


def test_kap_classifier_marks_brut_takas_as_blocking():
    assert classify_kap("Brüt takas tedbiri", None) == ("BRUT_TAKAS", "BLOCKING")


def test_kap_classifier_keeps_dividend_low_risk():
    assert classify_kap("Temettü dağıtım kararı", None) == ("DIVIDEND", "LOW")


def test_kap_classifier_marks_share_sale_as_medium_risk():
    assert classify_kap("Ortak pay satışı açıklaması", None) == ("SHARE_SALE", "MEDIUM")


def test_kap_date_alias_values_are_utc_aware():
    parsed = _published("2026-07-11T08:00:00Z")
    assert parsed == datetime(2026, 7, 11, 8, tzinfo=UTC)
    assert _published("not-a-date") is None


def test_kap_risk_window_excludes_old_and_unknown_dates():
    now = datetime(2026, 7, 11, 12, tzinfo=UTC)
    assert _is_active_risk(now - timedelta(hours=2), now=now, lookback_hours=24)
    assert not _is_active_risk(now - timedelta(days=10), now=now, lookback_hours=24)
    assert not _is_active_risk(None, now=now, lookback_hours=24)


async def test_kap_gateway_cache_avoids_second_call(monkeypatch):
    class Gateway:
        calls = 0

        async def get_kap(self, symbol, limit):
            self.calls += 1
            return {"ok": True, "news": []}

    gateway = Gateway()
    kap_service.invalidate_kap_cache()
    monkeypatch.setattr(kap_service, "gateway_client", gateway)
    await kap_service.sync_kap_events("THYAO")
    await kap_service.sync_kap_events("THYAO")
    assert gateway.calls == 1
