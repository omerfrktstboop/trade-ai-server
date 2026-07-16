"""Tool registry güvenlik sözleşmesi testleri (v2 Faz 1).

Kritik invariantlar:
- registry'de yazma yeteneği olan hiçbir araç yok (emir/config/kill switch)
- DeepSeek çağrıları sembol kapsamına ve audience'a hapsedilir
- call_tool asla exception fırlatmaz; timeout/validasyon hataları dict döner
- her çağrı tool_call_audits'e yazılır
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import select

import app.tools  # noqa: F401 — katalog araçlarını registry'ye yükler
from app.db.base import Base
from app.db.session import async_session_factory, engine
from app.models.db.tool_call_audit import ToolCallAudit
from app.tools.registry import (
    ERROR_AUDIENCE,
    ERROR_SYMBOL_SCOPE,
    ERROR_UNKNOWN_TOOL,
    REGISTRY,
    call_tool,
    tool,
)
from app.tools.openai_format import openai_tool_definitions


class FakeGatewayClient:
    """Katalog araçlarının çağırdığı client yüzeyinin minimal sahtesi."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def get_snapshot(self, symbol: str, requested_timeframe: str | None = None):
        self.calls.append(("get_snapshot", symbol))
        return {"ok": True, "payload": {"lastPrice": 71.5, "symbol": symbol}}

    async def get_depth(self, symbol: str, levels: int = 25):
        self.calls.append(("get_depth", (symbol, levels)))
        return {"ok": True, "symbol": symbol, "levels": levels}

    async def get_account(self):
        return {
            "ok": True,
            "accountId": "1234567",
            "name": "Gerçek İsim",
            "balance": 100000.0,
            "nested": {"userId": "u-99", "availableMargin": 5000.0},
        }

    async def get_positions(self):
        return {"ok": True, "positions": []}


@pytest.fixture(autouse=True)
async def _tables():
    import app.models.db  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest.fixture
def fake_gateway(monkeypatch: pytest.MonkeyPatch) -> FakeGatewayClient:
    fake = FakeGatewayClient()
    monkeypatch.setattr("app.services.matriks_gateway.gateway_client", fake)
    return fake


async def _audit_rows(tool_name: str) -> list[ToolCallAudit]:
    async with async_session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ToolCallAudit)
                    .where(ToolCallAudit.tool_name == tool_name)
                    .order_by(ToolCallAudit.id)
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


# ── Whitelist güvenlik sözleşmesi ───────────────────────────────────────────


def test_registry_contains_no_write_capable_tools():
    forbidden_fragments = ("order", "cancel", "send", "config", "kill", "switch", "set_")
    for name in REGISTRY:
        lowered = name.lower()
        assert not any(frag in lowered for frag in forbidden_fragments), (
            f"Registry'de yazma çağrışımlı araç adı: {name}"
        )


def test_expected_whitelist_tools_registered():
    expected_ai = {
        "get_snapshot",
        "get_bars",
        "get_depth",
        "get_indicators",
        "get_news",
        "get_kap",
        "get_institutions",
        "get_position",
    }
    expected_mcp_only = {
        "get_positions",
        "get_real_positions",
        "get_account_summary",
        "get_movers",
    }
    assert expected_ai | expected_mcp_only <= set(REGISTRY)
    for name in expected_ai:
        assert "ai" in REGISTRY[name].audience
    for name in expected_mcp_only:
        assert REGISTRY[name].audience == frozenset({"mcp"})


def test_openai_definitions_only_expose_ai_audience():
    definitions = openai_tool_definitions("ai")
    names = {d["function"]["name"] for d in definitions}
    assert "get_snapshot" in names
    assert "get_account_summary" not in names
    assert "get_positions" not in names

    bars = next(d for d in definitions if d["function"]["name"] == "get_bars")
    params = bars["function"]["parameters"]
    assert "symbol" in params["properties"]
    assert "symbol" in params.get("required", [])
    assert params["properties"]["count"]["default"] == 100


# ── call_tool güvenlik davranışları ─────────────────────────────────────────


async def test_unknown_tool_returns_error_dict():
    result = await call_tool("does_not_exist", {}, caller="mcp")
    assert ERROR_UNKNOWN_TOOL in result["error"]


async def test_validation_error_returns_error_dict(fake_gateway: FakeGatewayClient):
    result = await call_tool("get_depth", {"symbol": "THYAO", "levels": "abc"}, caller="mcp")
    assert "invalid arguments" in result["error"]
    assert fake_gateway.calls == []


async def test_deepseek_requires_matching_symbol_scope(fake_gateway: FakeGatewayClient):
    result = await call_tool(
        "get_snapshot", {"symbol": "AKBNK"}, caller="deepseek", symbol_scope="THYAO"
    )
    assert ERROR_SYMBOL_SCOPE in result["error"]

    result = await call_tool("get_snapshot", {"symbol": "AKBNK"}, caller="deepseek")
    assert ERROR_SYMBOL_SCOPE in result["error"]
    assert fake_gateway.calls == []

    ok = await call_tool(
        "get_snapshot", {"symbol": "THYAO"}, caller="deepseek", symbol_scope="THYAO"
    )
    assert ok["result"]["payload"]["lastPrice"] == 71.5


async def test_related_symbol_is_within_scope(fake_gateway: FakeGatewayClient):
    # RELATED_SYMBOLS: ANELE değerlendirmesi THYAO verisi isteyebilir.
    result = await call_tool(
        "get_snapshot", {"symbol": "THYAO"}, caller="deepseek", symbol_scope="ANELE"
    )
    assert "error" not in result


async def test_mcp_only_tool_rejected_for_deepseek(fake_gateway: FakeGatewayClient):
    result = await call_tool(
        "get_account_summary", {}, caller="deepseek", symbol_scope="THYAO"
    )
    assert ERROR_AUDIENCE in result["error"]


async def test_account_summary_masks_identity_fields(fake_gateway: FakeGatewayClient):
    result = await call_tool("get_account_summary", {}, caller="mcp")
    summary = result["result"]
    assert summary["accountId"] == "12***"
    assert summary["name"] == "Ge***"
    assert summary["nested"]["userId"] == "u-***"
    assert summary["balance"] == 100000.0
    assert summary["nested"]["availableMargin"] == 5000.0


async def test_timeout_returns_error_dict():
    @tool("zz_slow_probe", "test", timeout_seconds=0.05)
    async def zz_slow_probe() -> dict:
        await asyncio.sleep(0.5)
        return {}

    try:
        result = await call_tool("zz_slow_probe", {}, caller="mcp")
        assert "timed out" in result["error"]
    finally:
        REGISTRY.pop("zz_slow_probe", None)


async def test_oversized_result_is_truncated():
    @tool("zz_big_probe", "test", max_result_chars=32)
    async def zz_big_probe() -> dict:
        return {"blob": "x" * 500}

    try:
        result = await call_tool("zz_big_probe", {}, caller="mcp")
        assert result["truncated"] is True
        assert len(result["result"]) == 32
    finally:
        REGISTRY.pop("zz_big_probe", None)


async def test_handler_exception_returns_error_dict(monkeypatch: pytest.MonkeyPatch):
    class ExplodingClient(FakeGatewayClient):
        async def get_snapshot(self, symbol, requested_timeframe=None):
            raise RuntimeError("gateway down")

    monkeypatch.setattr("app.services.matriks_gateway.gateway_client", ExplodingClient())
    result = await call_tool("get_snapshot", {"symbol": "THYAO"}, caller="mcp")
    assert "tool failed" in result["error"]


# ── Audit ───────────────────────────────────────────────────────────────────


async def test_successful_call_writes_audit_row(fake_gateway: FakeGatewayClient):
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    await call_tool(
        "get_snapshot",
        {"symbol": "THYAO"},
        caller="deepseek",
        symbol_scope="THYAO",
        request_id=request_id,
    )
    rows = [r for r in await _audit_rows("get_snapshot") if r.request_id == request_id]
    assert len(rows) == 1
    row = rows[0]
    assert row.ok is True
    assert row.caller == "deepseek"
    assert row.symbol_scope == "THYAO"
    assert row.result_chars > 0
    assert '"symbol"' in row.args_json


async def test_blocked_call_also_writes_audit_row(fake_gateway: FakeGatewayClient):
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    await call_tool(
        "get_account_summary",
        {},
        caller="deepseek",
        symbol_scope="THYAO",
        request_id=request_id,
    )
    rows = [
        r
        for r in await _audit_rows("get_account_summary")
        if r.request_id == request_id
    ]
    assert len(rows) == 1
    assert rows[0].ok is False
    assert ERROR_AUDIENCE in rows[0].error
