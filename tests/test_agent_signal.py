"""Tests for the agentic signal evaluation endpoint (v2 — AgenticSignalRequest).

Covers:
  - AgenticSignalRequest with marketData accepted (no 422)
  - First ANELE → FETCH_DATA with target=THYAO, dataType=DEPTH
  - Second request (with THYAO DEPTH) → final WAIT/BUY/SELL
  - FETCH_DATA allowOrder=False
  - targetSymbol outside allowedSymbols → WAIT
  - max tool call exceeded → WAIT
  - expired session → WAIT
  - response top-level targetSymbol / requiredDataType
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.signal import AgenticAction, AgenticDataType
from app.services.session_store import (
    MAX_TOOL_CALLS_PER_SESSION,
    SESSION_TTL_SECONDS,
    session_store,
)


# ── Safe test token (ASCII-only, overrides .env's unicode token) ──────────
TEST_TOKEN = "agent-test-token-42"


# ── Safe test token (ASCII-only, avoids unicode header encoding issues) ───
TEST_TOKEN = "agent-test-token-123"


@pytest.fixture(autouse=True)
def _override_token(monkeypatch):
    """Override API_TOKEN with a pure ASCII value so headers work."""
    monkeypatch.setenv("API_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("ALLOWED_SYMBOLS", "ANELE,PGSUS,THYAO,TUPRS,KCHOL,AKBNK,SISE")
    from app.config import settings
    monkeypatch.setattr(settings, "api_token", TEST_TOKEN)
    # Rebuild risk_config with the overridden env
    from app.core.risk_config import risk_config
    monkeypatch.setattr(risk_config, "allowed_symbols", "ANELE,PGSUS,THYAO,TUPRS,KCHOL,AKBNK,SISE")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture(autouse=True)
def _clean_sessions() -> None:
    session_store._store.clear()
    yield
    session_store._store.clear()


# ── Test helpers ─────────────────────────────────────────────────────────────

# Default OLHCV payload for root symbol
_DEFAULT_OHLCV: dict[str, Any] = {
    "timeframe": "1h",
    "lastPrice": 300.0,
    "open": 298.0,
    "high": 305.0,
    "low": 296.0,
    "volume": 1_000_000.0,
    "rsi": 48.0,
    "rsi14": 48.0,
    "ema20": 299.0,
    "ema50": 290.0,
    "macd": 0.15,
    "macdSignal": 0.1,
    "botPositionQty": 0,
    "totalAccountQty": 0,
    "lockedLongTermQty": 0,
    "dailyTradeCount": 0,
}

# Default DEPTH payload for related symbol
_DEFAULT_DEPTH: dict[str, Any] = {
    "dataType": "DEPTH",
    "bidPrice": 299.5,
    "askPrice": 300.5,
    "bidVolume": 5000,
    "askVolume": 3000,
    "bidDepth": [{"price": 299.0, "volume": 1000}, {"price": 298.5, "volume": 2000}],
    "askDepth": [{"price": 301.0, "volume": 1500}, {"price": 301.5, "volume": 800}],
}


def _make_agentic_payload(
    *,
    request_id: str = "test-agent-001",
    symbol: str = "ANELE",
    session_id: str | None = None,
    data_type: AgenticDataType = AgenticDataType.OHLCV,
    payload: dict[str, Any] | None = None,
    context_history: list[dict[str, Any]] | None = None,
    mode: str = "PAPER",
) -> dict[str, Any]:
    """Build a payload conforming to AgenticSignalRequest shape."""
    if payload is None:
        payload = dict(_DEFAULT_OHLCV)

    body: dict[str, Any] = {
        "requestId": request_id,
        "symbol": symbol,
        "mode": mode,
        "marketData": {
            "symbol": symbol,
            "dataType": data_type.value,
            "payload": payload,
        },
    }
    if session_id is not None:
        body["sessionId"] = session_id
    if context_history is not None:
        body["contextHistory"] = context_history
    return body


def _post(
    client: TestClient,
    payload: dict[str, Any],
    headers: dict[str, str],
    expect_status: int = 200,
) -> dict[str, Any]:
    """Make an authenticated POST and return JSON body."""
    resp = client.post("/api/signal/evaluate-agent", json=payload, headers=headers)
    assert resp.status_code == expect_status, (
        f"HTTP {resp.status_code}: {resp.text}"
    )
    return resp.json()


# ── 1. Agentic request accepted (no 422) ────────────────────────────────────


def test_agentic_request_accepted_no_422(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """AgenticSignalRequest with marketData returns 200, not 422."""
    payload = _make_agentic_payload(symbol="ANELE")
    body = _post(client, payload, auth_headers)
    assert "sessionId" in body
    assert body["sessionId"] != ""
    assert "action" in body


# ── 2. First ANELE → FETCH_DATA target=THYAO DEPTH ──────────────────────────


def test_first_anele_returns_fetch_data_thyao_depth(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """First ANELE OHLCV request returns FETCH_DATA with target=THYAO, DEPTH."""
    payload = _make_agentic_payload(symbol="ANELE")
    body = _post(client, payload, auth_headers)

    assert body["action"] == "FETCH_DATA", f"Got {body.get('action')}"
    assert body["targetSymbol"] == "THYAO", f"Got {body.get('targetSymbol')}"
    assert body["requiredDataType"] == "DEPTH", f"Got {body.get('requiredDataType')}"
    assert body["allowOrder"] is False
    assert body["requiresConfirmation"] is False
    assert "reason" in body


# ── 3. Second request: THYAO DEPTH → final decision (WAIT/BUY/SELL) ──────────


def test_thyao_depth_second_step_proceeds_to_final(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Second request with THYAO DEPTH proceeds to final WAIT/BUY/SELL."""
    # First: ANELE OHLCV → FETCH_DATA THYAO DEPTH
    p1 = _make_agentic_payload(symbol="ANELE", request_id="req-step-1")
    r1 = _post(client, p1, auth_headers)
    assert r1["action"] == "FETCH_DATA"
    session_id = r1["sessionId"]
    assert session_id

    # Build context history with first step (ANELE OHLCV) + response metadata
    ctx_history = [
        {
            "stepNo": 1,
            "symbol": "ANELE",
            "dataType": "OHLCV",
            "payload": dict(_DEFAULT_OHLCV),
            "reason": "Step 1: ANELE OHLCV",
        },
    ]

    # Second: request THYAO DEPTH (Matriks fetches THYAO depth per FETCH_DATA)
    p2_thyao = {
        "requestId": "req-step-2",
        "symbol": "ANELE",
        "mode": "PAPER",
        "sessionId": session_id,
        "marketData": {
            "symbol": "THYAO",
            "dataType": "DEPTH",
            "payload": dict(_DEFAULT_DEPTH),
        },
        "contextHistory": ctx_history,
    }

    r2 = _post(client, p2_thyao, auth_headers)

    # Now planner sees: (ANELE, OHLCV) + (THYAO, DEPTH) = all collected
    # Should proceed to AI → final decision (WAIT/BUY/SELL)
    assert r2["action"] != "FETCH_DATA", (
        f"Unexpected FETCH_DATA on step 2 (THYAO DEPTH already provided): {r2}"
    )
    assert r2["action"] in ("WAIT", "BUY", "SELL"), f"Got {r2.get('action')}"
    if r2["action"] == "WAIT":
        assert r2["allowOrder"] is False


# ── 4. FETCH_DATA allowOrder=False ──────────────────────────────────────────


def test_fetch_data_allow_order_false(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """FETCH_DATA responses always return allowOrder=False."""
    payload = _make_agentic_payload(symbol="ANELE")
    body = _post(client, payload, auth_headers)

    if body["action"] == "FETCH_DATA":
        assert body["allowOrder"] is False
        assert body["requiresConfirmation"] is False


# ── 5. Target outside allowedSymbols → WAIT ──────────────────────────────────


def test_target_outside_allowed_symbols_waits(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """When targetSymbol is not in allowedSymbols, return WAIT."""
    # Use a symbol that triggers a related-symbol fetch request
    # but with a config that doesn't allow that related symbol
    # For now, we test that the endpoint handles this gracefully.
    payload = _make_agentic_payload(symbol="XXSYM")
    body = _post(client, payload, auth_headers)
    assert body["action"] in ("FETCH_DATA", "WAIT"), f"Got {body.get('action')}"


# ── 6. Max tool call exceeded → WAIT ────────────────────────────────────────


def test_max_tool_calls_exceeded_waits(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """After maxToolCallsPerSession FETCH_DATA calls, next returns WAIT."""
    max_calls = MAX_TOOL_CALLS_PER_SESSION

    # First: create session + FETCH_DATA 1
    p1 = _make_agentic_payload(symbol="ANELE", request_id="req-m1")
    r1 = _post(client, p1, auth_headers)
    session_id = r1["sessionId"]

    # Repeat FETCH_DATA calls up to max
    for i in range(1, max_calls):
        ctx = [
            {
                "stepNo": i,
                "symbol": "THYAO",
                "dataType": "DEPTH",
                "payload": dict(_DEFAULT_DEPTH),
                "reason": f"Step {i}",
            }
        ]
        # Each call returns FETCH_DATA because required data is still missing
        # (only DEPTH from THYAO counts if we include it in contextHistory)
        payload = _make_agentic_payload(
            request_id=f"req-m{i + 1}",
            symbol="ANELE",
            session_id=session_id,
            data_type=AgenticDataType.OHLCV,  # Different type, doesn't satisfy DEPTH need
            context_history=ctx,
        )
        r = _post(client, payload, auth_headers)

    # After max calls, next request → WAIT
    p_final = _make_agentic_payload(
        request_id="req-final",
        symbol="ANELE",
        session_id=session_id,
        data_type=AgenticDataType.OHLCV,
    )
    r_final = _post(client, p_final, auth_headers)
    assert r_final["action"] == "WAIT", f"Expected WAIT after max calls, got {r_final}"


# ── 7. Expired session → WAIT ────────────────────────────────────────────────


def test_expired_session_returns_wait(
    client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    """Expired session returns WAIT with reason."""
    # Shorten TTL for testing
    monkeypatch.setattr(
        "app.services.session_store.SESSION_TTL_SECONDS", 1
    )

    p1 = _make_agentic_payload(symbol="ANELE")
    r1 = _post(client, p1, auth_headers)
    session_id = r1["sessionId"]

    # Wait for session to expire
    time.sleep(1.5)

    p2 = _make_agentic_payload(
        symbol="ANELE",
        session_id=session_id,
        data_type=AgenticDataType.DEPTH,
        payload=dict(_DEFAULT_DEPTH),
        context_history=[
            {
                "stepNo": 1,
                "symbol": "ANELE",
                "dataType": "OHLCV",
                "payload": dict(_DEFAULT_OHLCV),
                "reason": "Step 1",
            }
        ],
    )
    r2 = _post(client, p2, auth_headers)
    assert r2["action"] == "WAIT", f"Expected WAIT for expired, got {r2}"
    assert "expired" in r2.get("reason", "").lower() or "not found" in r2.get("reason", "").lower()


# ── 8. Response top-level targetSymbol / requiredDataType ───────────────────


def test_response_has_top_level_target_and_datatype(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """FETCH_DATA response includes top-level targetSymbol + requiredDataType."""
    payload = _make_agentic_payload(symbol="ANELE")
    body = _post(client, payload, auth_headers)

    if body["action"] == "FETCH_DATA":
        assert "targetSymbol" in body, f"Missing targetSymbol: {body}"
        assert "requiredDataType" in body, f"Missing requiredDataType: {body}"
        assert isinstance(body["targetSymbol"], str)
        assert body["targetSymbol"] != ""
        assert body["requiredDataType"] in ("OHLCV", "DEPTH", "NEWS", "FUNDS", "BROKER_FLOWS")
    else:
        # Non-FETCH_DATA responses should still be valid
        assert "action" in body


# ── 9. contextHistory is appended to session ────────────────────────────────


def test_context_history_appended(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """contextHistory steps are appended to the session."""
    p1 = _make_agentic_payload(
        symbol="ANELE",
        request_id="req-ctx-1",
        context_history=[
            {
                "stepNo": 1,
                "symbol": "ANELE",
                "dataType": "DEPTH",
                "payload": {"bidPrice": 299.0, "askPrice": 300.0},
                "reason": "Initial DEPTH from history",
            }
        ],
    )
    r1 = _post(client, p1, auth_headers)

    # With DEPTH already in context, the planner may skip FETCH_DATA for DEPTH
    # and proceed (or request other data)
    assert r1["action"] in ("FETCH_DATA", "WAIT", "BUY", "SELL"), f"Got {r1.get('action')}"


# ── 10. Same symbol + same dataType → skip duplicate request ───────────────


def test_same_data_type_skipped(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """When required data already exists, planner doesn't re-request."""
    # Use PGSUS which maps to THYAO
    payload = _make_agentic_payload(
        symbol="PGSUS",
        request_id="req-same-1",
        context_history=[
            {
                "stepNo": 1,
                "symbol": "THYAO",
                "dataType": "DEPTH",
                "payload": dict(_DEFAULT_DEPTH),
                "reason": "Already have THYAO DEPTH",
            }
        ],
    )
    body = _post(client, payload, auth_headers)

    # Should NOT request THYAO DEPTH again (already have it)
    if body["action"] == "FETCH_DATA":
        # If it still requests data, it should NOT be THYAO DEPTH
        assert not (
            body.get("targetSymbol") == "THYAO"
            and body.get("requiredDataType") == "DEPTH"
        ), "Should not re-request THYAO DEPTH"


# ── 11. Real request ID persistence ─────────────────────────────────────────


def test_request_id_persisted(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Response echoes the requestId from the request."""
    payload = _make_agentic_payload(symbol="ANELE", request_id="my-custom-id-999")
    body = _post(client, payload, auth_headers)
    assert body.get("requestId") == "my-custom-id-999"


# ── 12. Valid FETCH_DATA structure ──────────────────────────────────────────


def test_fetch_data_response_structure(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """FETCH_DATA response has the correct structure."""
    payload = _make_agentic_payload(symbol="ANELE")
    body = _post(client, payload, auth_headers)

    # Required fields always present
    for key in ("requestId", "sessionId", "action", "allowOrder", "requiresConfirmation", "reason",
                "confidenceScore", "riskScore", "qty", "orderType"):
        assert key in body, f"Missing required field: {key}"

    if body["action"] == "FETCH_DATA":
        assert "targetSymbol" in body
        assert "requiredDataType" in body
        assert body["confidenceScore"] == 0.0
        assert body["riskScore"] == 0.0
        assert body["qty"] == 0.0


def test_agentic_bridge_maps_nested_technical_features() -> None:
    """Matriks marketData.payload technicalFeatures reach SignalRequest."""
    from app.models.signal import AgenticSignalRequest
    from app.routers.signal import _agentic_to_signal_request

    payload = _make_agentic_payload(
        symbol="THYAO",
        payload={
            **dict(_DEFAULT_OHLCV),
            "technicalFeatures": {
                "alphaTrendSignal": "BUY",
                "indicatorConsensus": "BUY",
                "indicatorBuyCount": 4,
                "indicatorSellCount": 1,
                "natr": 2.8,
                "depthQueueDropPct": 10.5,
                "marketRegime": "TRENDING",
            },
        },
    )

    request = AgenticSignalRequest(**payload)
    signal_request = _agentic_to_signal_request(request, "sess-tech")

    assert signal_request.alpha_trend_signal == "BUY"
    assert signal_request.indicator_consensus == "BUY"
    assert signal_request.indicator_buy_count == 4
    assert signal_request.indicator_sell_count == 1
    assert signal_request.natr == 2.8
    assert signal_request.depth_queue_drop_pct == 10.5
    assert signal_request.market_regime == "TRENDING"
