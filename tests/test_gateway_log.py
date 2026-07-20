from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

import app.routers.gateway_log as gateway_log_router
from app.config import settings
from app.main import app


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.effective_gateway_api_token}"}


def test_gateway_log_requires_scoped_authentication():
    response = TestClient(app).post(
        "/api/gateway-log",
        json={
            "timestamp": "2026-07-17T11:11:03Z",
            "level": "INFO",
            "message": "Gateway started",
        },
    )

    assert response.status_code == 401


def test_gateway_log_writes_json_line(tmp_path, monkeypatch):
    log_file = tmp_path / "logs" / "matriks.log"
    monkeypatch.setattr("app.core.logger.MATRIKS_LOG_FILE", log_file)

    response = TestClient(app).post(
        "/api/gateway-log",
        headers=_headers(),
        json={
            "timestamp": "2026-07-17T11:11:03Z",
            "level": "WARNING",
            "message": "Market data warning\nsecond line",
        },
    )

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    entry = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert entry["timestamp"] == "2026-07-17T11:11:03+00:00"
    assert entry["level"] == "WARNING"
    assert entry["source"] == "TradeAiGateway"
    assert entry["message"] == "Market data warning\nsecond line"
    received_at = datetime.fromisoformat(entry["receivedAt"])
    assert received_at.utcoffset().total_seconds() == 3 * 3600


def test_gateway_log_returns_503_when_file_write_fails(monkeypatch):
    def fail_write(**_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(gateway_log_router, "log_matriks_gateway", fail_write)
    response = TestClient(app).post(
        "/api/gateway-log",
        headers=_headers(),
        json={
            "timestamp": "2026-07-17T11:11:03Z",
            "level": "ERROR",
            "message": "write test",
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "gateway log persistence unavailable"


def test_gateway_source_forwards_safe_debug_without_trading_dependency():
    source = Path("matriks/TradeAiGateway.cs").read_text(encoding="utf-8")
    safe_debug = source.split("private void SafeDebug", 1)[1]
    safe_debug = safe_debug.split("// ── Internal types", 1)[0]

    assert 'Debug("[TradeAI Gateway] " + message)' in safe_debug
    assert "_ = PostGatewayLogAsync(message);" in safe_debug
    assert 'client.PostAsync("api/gateway-log", content)' in safe_debug
    assert "Logging is best-effort" in safe_debug
