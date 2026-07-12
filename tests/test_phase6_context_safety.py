import asyncio
from datetime import UTC, datetime, timedelta

from app.services.decision_gate import DecisionCache, decision_cache, decision_context_fingerprint
from app.services.news_service import _is_safe_public_http_url
from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import KapEvent
import app.services.kap_service as kap_service
from app.services.broker_flow_service import get_broker_flow_context
from app.routers.order_result import OrderResultRequest, record_order_result


def test_decision_cache_misses_when_any_context_fingerprint_changes():
    cache = DecisionCache()
    first = decision_context_fingerprint({"symbol": "THYAO", "position": 0, "configHash": "a"})
    second = decision_context_fingerprint({"symbol": "THYAO", "position": 1, "configHash": "a"})
    cache.put("THYAO", 100, None, {"action": "BUY"}, first)
    assert cache.get("THYAO", 100, None, first) is not None
    assert cache.get("THYAO", 100, None, second) is None


def test_buy_cache_default_ttl_is_at_most_15_seconds():
    cache = DecisionCache()
    assert cache._ttl <= timedelta(seconds=15)


async def test_fulltext_ssrf_guard_rejects_local_targets():
    assert not await _is_safe_public_http_url("http://127.0.0.1/admin")
    assert not await _is_safe_public_http_url("http://169.254.169.254/latest/meta-data")
    assert not await _is_safe_public_http_url("file:///etc/passwd")


def test_context_fingerprint_covers_akd_kap_and_timestamps():
    base = {"quoteEventUtc": datetime.now(UTC), "kapContext": {"risk": []}, "brokerFlowContext": {"smartMoneyFlow": "UNKNOWN"}, "profileCode": "NORMAL", "configHash": "1"}
    changed = dict(base, brokerFlowContext={"smartMoneyFlow": "STRONG_BUY"})
    assert decision_context_fingerprint(base) != decision_context_fingerprint(changed)


async def test_active_risky_kap_is_found_outside_latest_ten(monkeypatch):
    await drop_all(); await init_db()
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        session.add(KapEvent(symbol="THYAO", title="Active regulatory risk", event_type="REGULATORY_MEASURE", risk_level="BLOCKING", published_at=now - timedelta(hours=20)))
        for index in range(11):
            session.add(KapEvent(symbol="THYAO", title=f"Normal {index}", event_type="DIVIDEND", risk_level="LOW", published_at=now - timedelta(minutes=index)))
        await session.commit()
    class Gateway:
        async def get_kap(self, symbol, limit): return {"ok": True, "news": []}
    monkeypatch.setattr(kap_service, "gateway_client", Gateway())
    kap_service.invalidate_kap_cache()
    context = await kap_service.get_kap_context(["THYAO"])
    assert context["THYAO"]["hasBlockingRisk"] is True
    assert any(row["title"] == "Active regulatory risk" for row in context["THYAO"]["riskEvents24h"])


async def test_kap_and_akd_requests_are_single_flight(monkeypatch):
    class Gateway:
        kap_calls = 0
        akd_calls = 0
        async def get_kap(self, symbol, limit):
            self.kap_calls += 1; await asyncio.sleep(0.01); return {"ok": True, "news": []}
        async def get_institutions(self, symbol, **kwargs):
            self.akd_calls += 1; await asyncio.sleep(0.01); return {"available": False, "period": "DAILY"}
    gateway = Gateway()
    monkeypatch.setattr(kap_service, "gateway_client", gateway)
    kap_service.invalidate_kap_cache()
    await asyncio.gather(kap_service.sync_kap_events("THYAO"), kap_service.sync_kap_events("THYAO"))
    await asyncio.gather(get_broker_flow_context(["THYAO"], gateway=gateway, config_version="v1"), get_broker_flow_context(["THYAO"], gateway=gateway, config_version="v1"))
    assert gateway.kap_calls == 1
    assert gateway.akd_calls == 1


async def test_fill_event_invalidates_cached_buy(monkeypatch):
    await drop_all(); await init_db()
    fingerprint = decision_context_fingerprint({"position": 0})
    decision_cache.put("THYAO", 100, None, {"action": "BUY"}, fingerprint)
    async def no_notify(*args, **kwargs): return None
    monkeypatch.setattr("app.routers.order_result.notify_order_event", no_notify)
    await record_order_result(OrderResultRequest.model_validate({"requestId": "fill-cache", "symbol": "THYAO", "action": "BUY", "orderQty": 1, "filledQty": 1, "lastFillQty": 1, "avgPrice": 100, "limitPrice": 100, "status": "FILLED", "matriksMessage": "filled"}))
    assert decision_cache.get("THYAO", 100, None, fingerprint) is None
