"""MCP server sargısı testleri (v2 Faz 1).

FastMCP sunucusunun registry'den doğru beslendiğini ve araç çağrılarının
audit'li call_tool yolundan geçtiğini doğrular. HTTP katmanı (mount + token)
ayrıca bearer middleware testiyle kontrol edilir.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

import app.tools  # noqa: F401,E402 — katalog araçlarını yükler
from app.db.base import Base  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.tools.mcp_app import _BearerTokenMiddleware, build_mcp_server  # noqa: E402
from app.tools.registry import specs_for_audience  # noqa: E402


@pytest.fixture(autouse=True)
async def _tables():
    import app.models.db  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


class FakeGatewayClient:
    async def get_snapshot(self, symbol: str, requested_timeframe: str | None = None):
        return {"ok": True, "payload": {"lastPrice": 71.5, "symbol": symbol}}


async def test_mcp_server_exposes_exactly_the_mcp_audience():
    server = build_mcp_server()
    tools = await server.list_tools()
    exposed = {t.name for t in tools}
    expected = {spec.name for spec in specs_for_audience("mcp")}
    assert exposed == expected
    assert "get_account_summary" in exposed


async def test_mcp_tool_roundtrip_uses_call_tool(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "app.services.matriks_gateway.gateway_client", FakeGatewayClient()
    )
    server = build_mcp_server()
    result = await server.call_tool("get_snapshot", {"symbol": "THYAO"})
    text = str(result)
    assert "71.5" in text


async def test_bearer_middleware_blocks_missing_or_wrong_token():
    sent: list[dict] = []

    async def inner_app(scope, receive, send):  # pragma: no cover — ulaşılmamalı
        raise AssertionError("auth bypass!")

    async def send(message):
        sent.append(message)

    middleware = _BearerTokenMiddleware(inner_app, "correct-token")

    scope = {"type": "http", "headers": []}
    await middleware(scope, None, send)
    assert sent[0]["status"] == 401

    sent.clear()
    scope = {"type": "http", "headers": [(b"authorization", b"Bearer wrong")]}
    await middleware(scope, None, send)
    assert sent[0]["status"] == 401


async def test_bearer_middleware_passes_correct_token():
    passed: list[bool] = []

    async def inner_app(scope, receive, send):
        passed.append(True)

    middleware = _BearerTokenMiddleware(inner_app, "correct-token")
    scope = {"type": "http", "headers": [(b"authorization", b"Bearer correct-token")]}
    await middleware(scope, None, lambda m: None)
    assert passed == [True]
