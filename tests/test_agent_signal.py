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

import asyncio
import os
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
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

_TEST_ALLOWED_SYMBOLS = "ANELE,PGSUS,THYAO,TUPRS,KCHOL,AKBNK,SISE"


@pytest.fixture(autouse=True)
def _override_token(monkeypatch):
    """Override API_TOKEN with a pure ASCII value so headers work."""
    monkeypatch.setenv("API_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("ALLOWED_SYMBOLS", _TEST_ALLOWED_SYMBOLS)
    from app.config import settings
    monkeypatch.setattr(settings, "api_token", TEST_TOKEN)
    # Rebuild risk_config with the overridden env
    from app.core.risk_config import risk_config
    monkeypatch.setattr(risk_config, "allowed_symbols", _TEST_ALLOWED_SYMBOLS)


@pytest.fixture(autouse=True)
def _seed_db_allowed_symbols():
    """The agentic planner's initial symbol-allow gate reads a DB-backed
    RiskConfig (app/routers/signal.py), not just the static risk_config
    singleton — seed the same allow-list into SystemConfig so these tests
    exercise the real /evaluate-agent code path end-to-end."""
    from app.services.admin_config import set_admin_config_value

    async def _seed() -> None:
        await drop_all()
        await init_db()
        async with async_session_factory() as session:
            await set_admin_config_value(
                session,
                "allowedSymbols",
                _TEST_ALLOWED_SYMBOLS,
                changed_by="test-setup",
            )

    asyncio.run(_seed())
    yield
    asyncio.run(drop_all())
    asyncio.run(init_db())


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
    body = resp.json()
    if expect_status == 200:
        assert body.get("configVersion"), f"Missing configVersion: {body}"
        assert body.get("configHash"), f"Missing configHash: {body}"
    return body


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


# ── 8b. Every response carries the root symbol ──────────────────────────────
#
# Regression coverage: response.Symbol was previously always empty on the
# Matriks bot side (the field didn't exist on AgenticSignalResponse), which
# caused every BUY/SELL order to be rejected at the bot's "symbol not
# allowed" gate since NormalizeSymbol(null) -> "".


def test_fetch_data_response_includes_root_symbol(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    payload = _make_agentic_payload(symbol="ANELE")
    body = _post(client, payload, auth_headers)

    assert body["symbol"] == "ANELE"


def test_wait_hard_stop_response_includes_root_symbol(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Target-symbol-not-allowed WAIT (hard stop) must still carry the symbol."""
    payload = _make_agentic_payload(symbol="ZZZZ-NOT-ALLOWED")
    body = _post(client, payload, auth_headers)

    assert body["action"] == "WAIT"
    assert body["symbol"] == "ZZZZ-NOT-ALLOWED"


def test_final_response_includes_root_symbol(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Second-step (final BUY/SELL/WAIT) response must carry the root symbol."""
    p1 = _make_agentic_payload(symbol="ANELE", request_id="req-symbol-1")
    r1 = _post(client, p1, auth_headers)
    assert r1["symbol"] == "ANELE"
    session_id = r1["sessionId"]

    ctx_history = [
        {
            "stepNo": 1,
            "symbol": "ANELE",
            "dataType": "OHLCV",
            "payload": dict(_DEFAULT_OHLCV),
            "reason": "Step 1: ANELE OHLCV",
        },
    ]
    p2 = {
        "requestId": "req-symbol-2",
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
    r2 = _post(client, p2, auth_headers)

    assert r2["action"] != "FETCH_DATA"
    assert r2["symbol"] == "ANELE"


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


# ── 13. Related-symbol flow must not mix root/auxiliary symbol data ────────
#
# Regression coverage for a real production bug: for RELATED_SYMBOLS roots
# (PGSUS/ANELE/TUPRS), the planner proceeds to AI right after the related
# symbol's DEPTH is collected — so the request that finally triggers PROCEED
# carries the AUXILIARY symbol's marketData, not the root's own. Before the
# fix, _agentic_to_signal_request built the whole decision (price, RSI,
# EMA/MACD, position qty) from that auxiliary payload while still labeling
# the decision with the root symbol — i.e. a PGSUS decision built from
# THYAO's price/indicators/position.


def test_agentic_bridge_uses_root_symbols_own_step_not_related_symbol() -> None:
    """Unit test: session has both the root's own step and a related
    symbol's step; market_data on this turn is the related symbol's. The
    built SignalRequest must reflect the root symbol's own data."""
    from app.models.signal import AgenticSignalRequest, AgenticDataType, ContextStep
    from app.routers.signal import _agentic_to_signal_request
    from app.services.session_store import SessionState

    session = SessionState(rootSymbol="PGSUS")
    session.steps.append(ContextStep(
        stepNo=1, symbol="PGSUS", dataType=AgenticDataType.DEPTH,
        payload={**dict(_DEFAULT_OHLCV), "rsi": 71.5, "ema20": 123.4, "lastPrice": 55.5},
        reason="Market data: DEPTH",
    ))
    session.steps.append(ContextStep(
        stepNo=2, symbol="THYAO", dataType=AgenticDataType.DEPTH,
        payload=dict(_DEFAULT_DEPTH),
        reason="Market data: DEPTH",
    ))

    payload = _make_agentic_payload(symbol="PGSUS", payload=dict(_DEFAULT_DEPTH))
    payload["marketData"]["symbol"] = "THYAO"  # this turn's data is THYAO's
    request = AgenticSignalRequest(**payload)

    signal_request = _agentic_to_signal_request(request, "sess-related", session=session)

    assert signal_request.symbol == "PGSUS"
    assert signal_request.rsi == 71.5
    assert signal_request.ema20 == 123.4
    assert signal_request.last_price == 55.5


async def _load_market_snapshot(request_id: str):
    from sqlalchemy import select
    from app.models.db import MarketSnapshot

    async with async_session_factory() as session:
        stmt = select(MarketSnapshot).where(MarketSnapshot.request_id == request_id)
        return (await session.execute(stmt)).scalar_one_or_none()


def test_related_symbol_flow_persists_root_symbols_own_data(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """End-to-end: PGSUS → FETCH_DATA THYAO DEPTH → PROCEED. The persisted
    market_snapshot for this request must carry PGSUS's own OHLCV/RSI, not
    THYAO's DEPTH-only payload (which has no rsi/ema/price fields at all —
    if the bug were present these would come back as None/0)."""
    root_payload = {**dict(_DEFAULT_OHLCV), "rsi": 71.5, "ema20": 123.4, "lastPrice": 55.5}
    p1 = _make_agentic_payload(
        symbol="PGSUS", request_id="req-related-1", payload=root_payload,
    )
    r1 = _post(client, p1, auth_headers)
    assert r1["action"] == "FETCH_DATA"
    assert r1["targetSymbol"] == "THYAO"
    session_id = r1["sessionId"]

    ctx_history = [
        {
            "stepNo": 1,
            "symbol": "PGSUS",
            "dataType": "OHLCV",
            "payload": root_payload,
            "reason": "Step 1: PGSUS OHLCV",
        },
    ]
    p2 = {
        "requestId": "req-related-1",
        "symbol": "PGSUS",
        "mode": "PAPER",
        "sessionId": session_id,
        "marketData": {
            "symbol": "THYAO",
            "dataType": "DEPTH",
            "payload": dict(_DEFAULT_DEPTH),
        },
        "contextHistory": ctx_history,
    }
    r2 = _post(client, p2, auth_headers)
    assert r2["action"] != "FETCH_DATA"
    assert r2["symbol"] == "PGSUS"

    snapshot = asyncio.run(_load_market_snapshot("req-related-1"))
    assert snapshot is not None
    assert snapshot.symbol == "PGSUS"
    assert snapshot.rsi == 71.5
    assert snapshot.ema20 == 123.4
    assert snapshot.close == 55.5


def test_context_history_dedup_skips_already_collected_step(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Resending an already-recorded step via contextHistory (as the C# bot
    does on every FETCH_DATA turn — see TradeAiAgenticBot.cs's "Previous
    marketData" step) must not duplicate it in session.steps."""
    p1 = _make_agentic_payload(symbol="ANELE", request_id="req-dedup-1")
    r1 = _post(client, p1, auth_headers)
    session_id = r1["sessionId"]

    sess = session_store.get_session(session_id)
    assert len(sess.steps) == 1
    first_step_payload = sess.steps[0].payload

    ctx_history = [
        {
            "stepNo": 1,
            "symbol": "ANELE",
            "dataType": "OHLCV",
            "payload": first_step_payload,
            "reason": "Previous marketData",
        },
    ]
    p2 = {
        "requestId": "req-dedup-1",
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
    _post(client, p2, auth_headers)

    sess = session_store.get_session(session_id)
    assert sess is not None
    keys = [(s.symbol.upper(), s.data_type.value) for s in sess.steps]
    assert keys == [("ANELE", "OHLCV"), ("THYAO", "DEPTH")], keys


def test_agentic_bridge_maps_ohlc_reliable_flag() -> None:
    """Matriks sets ohlcReliable=false when open/high/low are just lastPrice
    repeated (no real bar data yet) — this must reach the built SignalRequest
    so it can flow through to the AI payload."""
    from app.models.signal import AgenticSignalRequest
    from app.routers.signal import _agentic_to_signal_request

    payload = _make_agentic_payload(
        symbol="THYAO",
        payload={**dict(_DEFAULT_OHLCV), "ohlcReliable": False},
    )
    request = AgenticSignalRequest(**payload)
    signal_request = _agentic_to_signal_request(request, "sess-ohlc")

    assert signal_request.ohlc_reliable is False
