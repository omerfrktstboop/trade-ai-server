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
