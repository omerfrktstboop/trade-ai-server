"""Tests for the agentic signal evaluation endpoint."""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models.signal import AgentAction
from app.services.agent_session import (
    MAX_TOOL_CALLS_PER_SESSION,
    SESSION_TTL_SECONDS,
    AgentSession,
    agent_session_store,
)


@pytest.fixture
def client() -> TestClient:
    """Provide a FastAPI test client."""
    return TestClient(app)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer token header for protected endpoints."""
    return {"Authorization": f"Bearer {settings.api_token}"}


@pytest.fixture(autouse=True)
def _clean_sessions() -> None:
    """Clean session store before each test."""
    agent_session_store._store.clear()
    yield
    agent_session_store._store.clear()


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_payload(
    symbol: str = "THYAO",
    session_id: str | None = None,
    mode: str = "PAPER",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "requestId": "test-agent-001",
        "symbol": symbol,
        "timeframe": "1h",
        "mode": mode,
        "lastPrice": 100.0,
        "open": 99.0,
        "high": 102.0,
        "low": 98.0,
        "bidPrice": 99.9,
        "askPrice": 100.1,
        "volume": 500000.0,
        "dailyChangePct": 0.0,
        "rsi14": 45.0,
        "macdSignal": 0.1,
    }
    if session_id is not None:
        body["sessionId"] = session_id
    return body


def _post(
    client: TestClient, payload: dict[str, Any], headers: dict[str, str]
) -> Any:
    """Make an authenticated POST and return the JSON body."""
    resp = client.post(
        "/api/signal/evaluate-agent",
        json=payload,
        headers=headers,
    )
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    return resp.json()


# ── Test: first turn returns FETCH_DATA ──────────────────────────────────────


def test_first_turn_returns_fetch_data(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """The very first call (no session) should return FETCH_DATA."""
    body = _post(client, _make_payload(), auth_headers)

    assert body["action"] == "FETCH_DATA"
    assert body["fetchData"] is not None
    assert "sessionId" in body
    assert body["sessionId"] != ""
    assert body["allowOrder"] is False
    assert body["fetchData"]["targetSymbol"] == "THYAO"


# ── Test: second turn (same session, feed data) ──────────────────────────────


def test_second_turn_with_context(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """A second call with the same session should advance the planner."""
    # First turn: get session + FETCH_DATA
    body1 = _post(client, _make_payload(), auth_headers)
    sid = body1["sessionId"]

    # Second turn: same session, feed back with market data
    body2 = _post(client, _make_payload(session_id=sid), auth_headers)

    # Should advance: FETCH_DATA for next data type or final
    assert body2["sessionId"] == sid
    assert body2["action"] in ("FETCH_DATA", "WAIT", "BUY", "SELL")


# ── Test: max tool calls exhausted ───────────────────────────────────────────


def test_max_tool_calls_to_final(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """After MAX_TOOL_CALLS, the planner delegates to AI (WAIT / BUY / SELL)."""
    body1 = _post(client, _make_payload(), auth_headers)
    sid = body1["sessionId"]

    # Exhaust all tool calls
    for _ in range(MAX_TOOL_CALLS_PER_SESSION):
        _post(client, _make_payload(session_id=sid), auth_headers)

    # Next call should trigger final decision (WAIT / BUY / SELL)
    body_final = _post(client, _make_payload(session_id=sid), auth_headers)
    assert body_final["action"] in ("WAIT", "BUY", "SELL"), (
        f"Expected final action, got {body_final['action']}"
    )


# ── Test: session TTL expiry ─────────────────────────────────────────────────


def test_session_ttl_expiry(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """When a session expires, the endpoint returns WAIT."""
    body1 = _post(client, _make_payload(), auth_headers)
    sid = body1["sessionId"]

    # Force-expire the session
    session = agent_session_store.get(sid, "THYAO")
    assert session is not None
    session.created_at = time.monotonic() - SESSION_TTL_SECONDS - 10

    body2 = _post(client, _make_payload(session_id=sid), auth_headers)
    assert body2["action"] == "WAIT"
    assert "expired" in body2.get("reason", "").lower()


# ── Test: disallowed symbol ──────────────────────────────────────────────────


def test_disallowed_symbol_returns_wait(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Symbol not in allowed list should return WAIT immediately."""
    body = _post(client, _make_payload(symbol="BTCUSDT"), auth_headers)
    assert body["action"] == "WAIT"
    assert "not in the allowed list" in body.get("reason", "").lower()


# ── Test: FETCH_DATA blocks order creation ───────────────────────────────────


def test_fetch_data_blocks_order(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """When action is FETCH_DATA, allowOrder must be False."""
    body = _post(client, _make_payload(), auth_headers)
    assert body["action"] == "FETCH_DATA"
    assert body["allowOrder"] is False


# ── Test: independent sessions ───────────────────────────────────────────────


def test_independent_sessions(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Two symbols should create two independent sessions."""
    body1 = _post(client, _make_payload(symbol="THYAO"), auth_headers)
    body2 = _post(client, _make_payload(symbol="AKBNK"), auth_headers)

    s1 = body1["sessionId"]
    s2 = body2["sessionId"]
    assert s1 != s2


# ── Test: SessionStore cleanup ───────────────────────────────────────────────


def test_store_cleanup_removes_expired() -> None:
    """Expired sessions should be cleaned up automatically."""
    store = agent_session_store
    store._store.clear()

    s1 = store.create("uuid1", "THYAO", "PAPER")
    s2 = store.create("uuid2", "AKBNK", "PAPER")

    # Expire s1
    s1.created_at = time.monotonic() - SESSION_TTL_SECONDS - 1
    assert s1.is_expired

    # get() on expired should return None and clean
    assert store.get(s1.session_id, s1.symbol) is None
    assert store._key(s1.session_id, s1.symbol) not in store._store

    # s2 still alive
    assert store.get(s2.session_id, s2.symbol) is not None


# ── Test: AgentSession.add_context merges dictionaries ───────────────────────


def test_session_context_merge() -> None:
    """add_context merges nested dicts on the same key."""
    session = AgentSession(session_id="test", symbol="X", mode="PAPER")
    session.add_context("prices", {"open": 100})
    session.add_context("prices", {"close": 102})
    assert session.context_data["prices"] == {"open": 100, "close": 102}

    session.add_context("volume", 5000)
    session.add_context("volume", 6000)
    assert session.context_data["volume"] == 6000


# ── Test: unauthenticated request returns 401 ────────────────────────────────


def test_unauthenticated_request(client: TestClient) -> None:
    """Missing Bearer token should return 401."""
    resp = client.post("/api/signal/evaluate-agent", json=_make_payload())
    assert resp.status_code == 401
    assert "Not authenticated" in resp.json()["detail"]


# ── v2 Pydantic model tests ──────────────────────────────────────────────────


def test_agentic_data_type_enum() -> None:
    """AgenticDataType must have all 7 values."""
    from app.models.signal import AgenticDataType

    expected = {"DEPTH", "AKD", "OHLCV", "TECHNICAL", "NEWS", "FUND", "BROKER_FLOW"}
    actual = {e.value for e in AgenticDataType}
    assert actual == expected


def test_agentic_action_enum() -> None:
    """AgenticAction must have BUY, SELL, WAIT, FETCH_DATA."""
    from app.models.signal import AgenticAction

    expected = {"BUY", "SELL", "WAIT", "FETCH_DATA"}
    actual = {e.value for e in AgenticAction}
    assert actual == expected


def test_market_data_payload() -> None:
    """MarketDataPayload serialization and camelCase aliases."""
    from app.models.signal import AgenticDataType, MarketDataPayload

    m = MarketDataPayload(
        symbol="THYAO",
        dataType="DEPTH",
        payload={"bid": 100, "ask": 101},
    )
    assert m.symbol == "THYAO"
    assert m.data_type == AgenticDataType.DEPTH
    assert m.payload == {"bid": 100, "ask": 101}
    assert m.timestamp is None

    # Serialize → camelCase
    j = m.model_dump(by_alias=True)
    assert j["dataType"] == "DEPTH"
    assert j["payload"] == {"bid": 100, "ask": 101}
    assert j["timestamp"] is None


def test_market_data_payload_with_timestamp() -> None:
    """MarketDataPayload with optional timestamp."""
    from datetime import datetime, timezone
    from app.models.signal import MarketDataPayload

    ts = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)
    m = MarketDataPayload(
        symbol="THYAO",
        dataType="OHLCV",
        payload={"close": 100.0},
        timestamp=ts,
    )
    assert m.timestamp == ts

    j = m.model_dump(by_alias=True)
    assert j["timestamp"] is not None


def test_context_step() -> None:
    """ContextStep model and camelCase aliases."""
    from app.models.signal import AgenticDataType, ContextStep

    c = ContextStep(
        stepNo=1,
        symbol="THYAO",
        dataType="NEWS",
        payload={"headline": "KAP bildirimi"},
    )
    assert c.step_no == 1
    assert c.symbol == "THYAO"
    assert c.data_type == AgenticDataType.NEWS
    assert c.payload == {"headline": "KAP bildirimi"}
    assert c.reason is None

    j = c.model_dump(by_alias=True)
    assert j["stepNo"] == 1
    assert j["dataType"] == "NEWS"


def test_context_step_with_reason() -> None:
    """ContextStep with optional reason field."""
    from app.models.signal import ContextStep

    c = ContextStep(
        stepNo=3,
        symbol="AKBNK",
        dataType="TECHNICAL",
        payload={"rsi": 70},
        reason="Overbought check",
    )
    assert c.reason == "Overbought check"


def test_agentic_signal_request() -> None:
    """AgenticSignalRequest with all required + optional fields."""
    from app.models.signal import AgenticSignalRequest, ContextStep, MarketDataPayload

    req = AgenticSignalRequest(
        requestId="req-001",
        sessionId="sess-abc",
        symbol="THYAO",
        marketData={
            "symbol": "THYAO",
            "dataType": "OHLCV",
            "payload": {"close": 100},
        },
        contextHistory=[
            {"stepNo": 1, "symbol": "THYAO", "dataType": "DEPTH", "payload": {}}
        ],
    )

    assert req.request_id == "req-001"
    assert req.session_id == "sess-abc"
    assert req.symbol == "THYAO"
    assert isinstance(req.market_data, MarketDataPayload)
    assert req.market_data.data_type.value == "OHLCV"
    assert len(req.context_history) == 1
    assert isinstance(req.context_history[0], ContextStep)
    assert req.context_history[0].step_no == 1

    # camelCase aliases
    j = req.model_dump(by_alias=True)
    assert j["requestId"] == "req-001"
    assert j["sessionId"] == "sess-abc"
    assert j["marketData"]["dataType"] == "OHLCV"
    assert len(j["contextHistory"]) == 1
    assert j["contextHistory"][0]["stepNo"] == 1


def test_agentic_signal_request_defaults() -> None:
    """AgenticSignalRequest defaults: None session_id, empty history, PAPER mode."""
    from app.models.signal import AgenticSignalRequest, SignalMode

    req = AgenticSignalRequest(
        requestId="req-002",
        symbol="AKBNK",
        marketData={
            "symbol": "AKBNK",
            "dataType": "AKD",
            "payload": {},
        },
    )
    assert req.session_id is None
    assert req.context_history == []
    assert req.mode == SignalMode.PAPER


def test_agentic_signal_response_buy() -> None:
    """AgenticSignalResponse for BUY action."""
    from app.models.signal import AgenticAction, AgenticSignalResponse

    resp = AgenticSignalResponse(
        requestId="req-001",
        sessionId="sess-abc",
        action="BUY",
        allowOrder=True,
        requiresConfirmation=False,
        reason="Strong signal with fund support",
        confidenceScore=0.85,
        riskScore=0.2,
        qty=1000,
        orderType="LIMIT",
        price=98.5,
        entryRange={"min": 98.0, "max": 99.0},
        stopLoss=95.0,
        targetPrice=110.0,
    )

    assert resp.action == AgenticAction.BUY
    assert resp.allow_order is True
    assert resp.requires_confirmation is False
    assert resp.qty == 1000
    assert resp.order_type.value == "LIMIT"
    assert resp.entry_range.min == 98.0
    assert resp.entry_range.max == 99.0
    assert resp.stop_loss == 95.0
    assert resp.target_price == 110.0


def test_agentic_signal_response_fetch_data() -> None:
    """AgenticSignalResponse for FETCH_DATA — no order fields."""
    from app.models.signal import AgenticAction, AgenticDataType, AgenticSignalResponse

    resp = AgenticSignalResponse(
        requestId="req-002",
        sessionId="sess-def",
        action="FETCH_DATA",
        allowOrder=False,
        requiresConfirmation=False,
        reason="Need broker flow data for THYAO",
        targetSymbol="THYAO",
        requiredDataType="BROKER_FLOW",
        confidenceScore=0.0,
        riskScore=0.0,
        qty=0,
        orderType="NONE",
    )

    assert resp.action == AgenticAction.FETCH_DATA
    assert resp.allow_order is False
    assert resp.target_symbol == "THYAO"
    assert resp.required_data_type == AgenticDataType.BROKER_FLOW
    assert resp.qty == 0
    assert resp.order_type.value == "NONE"
    assert resp.entry_range is None
    assert resp.stop_loss is None
    assert resp.target_price is None


def test_agentic_signal_response_camelcase_serialization() -> None:
    """AgenticSignalResponse serializes all fields to camelCase."""
    from app.models.signal import AgenticSignalResponse

    resp = AgenticSignalResponse(
        requestId="r99", sessionId="s99", action="WAIT",
        allowOrder=False, requiresConfirmation=True, reason="test",
        confidenceScore=0.1, riskScore=0.9, qty=0, orderType="NONE",
    )

    j = resp.model_dump(by_alias=True)
    assert j["requestId"] == "r99"
    assert j["sessionId"] == "s99"
    assert j["allowOrder"] is False
    assert j["requiresConfirmation"] is True
    assert j["confidenceScore"] == 0.1
    assert j["riskScore"] == 0.9
    assert j["orderType"] == "NONE"
    assert "targetSymbol" in j
    assert "requiredDataType" in j
    assert "entryRange" in j
    assert "stopLoss" in j
    assert "targetPrice" in j


def test_existing_models_unaffected() -> None:
    """Existing SignalResponse and AgentSignalResponse still work exactly as before."""
    from app.models.signal import (
        AgentAction,
        AgentSignalResponse,
        EntryRange,
        SignalAction,
        SignalResponse,
    )

    # Existing signal response
    s = SignalResponse(
        requestId="r1", symbol="T", action="BUY",
        qty=500, orderType="LIMIT", confidenceScore=0.8,
        riskScore=0.3, allowOrder=True, reason="test",
        entryRange={"min": 10, "max": 11}, stopLoss=9, targetPrice=14,
    )
    assert s.action == SignalAction.BUY
    assert s.qty == 500

    # Existing agent signal response
    a = AgentSignalResponse(
        requestId="r2", symbol="T", sessionId="s", action="FETCH_DATA",
        reason="needs data",
    )
    assert a.action == AgentAction.FETCH_DATA
    assert a.qty == 0.0

    # EntryRange
    e = EntryRange(min=5, max=6)
    assert e.min == 5
    assert e.max == 6
