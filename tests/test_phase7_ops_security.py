import pytest
import json
from fastapi.security import HTTPAuthorizationCredentials

from app.core.auth import verify_admin_token, verify_evaluation_token, verify_gateway_token
from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.services.admin_config import get_admin_config_value, set_admin_config_values
import app.routers.health as health_router


async def test_scoped_tokens_are_not_interchangeable(monkeypatch):
    from app.core.auth import settings
    monkeypatch.setattr(settings, "evaluation_api_token", "evaluation-secret")
    monkeypatch.setattr(settings, "gateway_api_token", "gateway-secret")
    monkeypatch.setattr(settings, "admin_api_token", "admin-secret")
    assert await verify_evaluation_token(HTTPAuthorizationCredentials(scheme="Bearer", credentials="evaluation-secret"))
    with pytest.raises(Exception):
        await verify_gateway_token(HTTPAuthorizationCredentials(scheme="Bearer", credentials="evaluation-secret"))
    assert await verify_admin_token(HTTPAuthorizationCredentials(scheme="Bearer", credentials="admin-secret"))


async def test_batch_config_rolls_back_all_values_on_validation_error():
    await drop_all(); await init_db()
    async with async_session_factory() as session:
        with pytest.raises(ValueError):
            await set_admin_config_values(session, {"botEnableDemoOrders": True, "botAllowMarketOrders": True}, changed_by="test")
    async with async_session_factory() as session:
        assert await get_admin_config_value(session, "botEnableDemoOrders") == "false"


def test_gateway_masks_account_ids_and_migration_merges_duplicates():
    from pathlib import Path
    root = Path(__file__).parents[1]
    gateway = (root / "matriks" / "TradeAiGateway.cs").read_text(encoding="utf-8")
    migration = (root / "migrations" / "versions" / "20260712_01_order_lifecycle.py").read_text(encoding="utf-8")
    assert "MaskAccountId" in gateway
    assert "HAVING COUNT(*) > 1" in migration
    assert "uq_order_logs_request_id" in migration
    assert all(name in migration for name in ("order_qty", "limit_price", "state", "error_message"))


async def test_live_and_ready_health_are_separate(monkeypatch):
    await drop_all(); await init_db()
    async def gateway_health():
        return {"ok": True, "configStale": False, "configVersion": "v1", "configAgeSeconds": 1, "runtimeMode": "DEMO_LIVE", "testAutoOrderEnabled": True, "orderLimits": {"demoAccountConfirmed": True}, "accountVerificationAgeSeconds": 1, "positionSyncAgeSeconds": 1, "quoteAgeSeconds": {"THYAO": 1}, "depthAgeSeconds": {"THYAO": 1}, "callbackQueueDepth": 0, "callbackOutboxBacklog": 0}
    monkeypatch.setattr(health_router.gateway_client, "health", gateway_health)
    live = await health_router.health_live()
    ready = await health_router.health_ready()
    assert live.status_code == 200
    assert ready.status_code == 200
    payload = json.loads(ready.body)
    assert payload["status"] == "ready"
    assert payload["checks"]["migration"]["version"] == "development-create-all"
