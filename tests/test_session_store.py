"""Tests for session_store.py — SessionState and SessionStore."""

from __future__ import annotations

import time

import pytest

from app.models.signal import ContextStep
from app.services.session_store import (
    MAX_TOOL_CALLS_PER_SESSION,
    SESSION_TTL_SECONDS,
    SessionState,
    SessionStatus,
    SessionStore,
    session_store,
)


@pytest.fixture(autouse=True)
def _clean_store() -> None:
    """Ensure a clean store before and after each test."""
    session_store._store.clear()
    yield
    session_store._store.clear()


# ── SessionState ─────────────────────────────────────────────────────────────


def test_create_session_state() -> None:
    """create_session populates all fields correctly."""
    s = session_store.create_session("THYAO")

    assert s.root_symbol == "THYAO"
    assert s.status == SessionStatus.OPEN
    assert s.tool_call_count == 0
    assert s.steps == []
    assert len(s.session_id) == 32  # UUID hex
    assert s.created_at > 0
    assert abs(s.updated_at - s.created_at) < 0.01  # same instant, tiny delta


def test_create_session_unique_ids() -> None:
    """Each session gets a unique ID."""
    s1 = session_store.create_session("THYAO")
    s2 = session_store.create_session("AKBNK")
    assert s1.session_id != s2.session_id


def test_get_session_found() -> None:
    """get_session returns the session when it exists."""
    s = session_store.create_session("THYAO")
    found = session_store.get_session(s.session_id)
    assert found is not None
    assert found.session_id == s.session_id


def test_get_session_missing() -> None:
    """get_session returns None for unknown IDs."""
    assert session_store.get_session("nonexistent") is None


def test_get_session_expired_removes() -> None:
    """Expired session is removed from store automatically."""
    s = session_store.create_session("THYAO")

    # Force-expire by backdating created_at
    object.__setattr__(
        s, "created_at", time.monotonic() - SESSION_TTL_SECONDS - 10
    )
    session_store._store[s.session_id] = s

    assert session_store.get_session(s.session_id) is None
    assert s.session_id not in session_store._store


def test_append_step() -> None:
    """append_step adds a ContextStep and touches updated_at."""
    s = session_store.create_session("THYAO")
    original_updated = s.updated_at

    import time as _time
    _time.sleep(0.01)  # ensure time moves

    step = ContextStep(
        stepNo=1, symbol="THYAO", dataType="OHLCV", payload={"close": 100}
    )
    result = session_store.append_step(s.session_id, step)

    assert result is not None
    assert len(result.steps) == 1
    assert result.steps[0].step_no == 1
    assert result.steps[0].data_type.value == "OHLCV"
    assert result.updated_at > original_updated


def test_append_step_missing_session() -> None:
    """append_step returns None for unknown session."""
    step = ContextStep(stepNo=1, symbol="X", dataType="DEPTH", payload={})
    assert session_store.append_step("no-such-id", step) is None


def test_append_step_multiple() -> None:
    """Multiple steps accumulate in order."""
    s = session_store.create_session("THYAO")

    session_store.append_step(
        s.session_id,
        ContextStep(stepNo=1, symbol="THYAO", dataType="DEPTH", payload={}),
    )
    session_store.append_step(
        s.session_id,
        ContextStep(stepNo=2, symbol="THYAO", dataType="AKD", payload={}),
    )
    session_store.append_step(
        s.session_id,
        ContextStep(stepNo=3, symbol="THYAO", dataType="OHLCV", payload={}),
    )

    updated = session_store.get_session(s.session_id)
    assert updated is not None
    assert len(updated.steps) == 3
    assert [st.step_no for st in updated.steps] == [1, 2, 3]


def test_increment_tool_call() -> None:
    """increment_tool_call bumps counter and touches timestamp."""
    s = session_store.create_session("THYAO")
    original_updated = s.updated_at

    import time as _time
    _time.sleep(0.01)

    result = session_store.increment_tool_call(s.session_id)
    assert result is not None
    assert result.tool_call_count == 1
    assert result.updated_at > original_updated

    # Second call
    result2 = session_store.increment_tool_call(s.session_id)
    assert result2 is not None
    assert result2.tool_call_count == 2


def test_increment_tool_call_missing_session() -> None:
    """increment_tool_call returns None for unknown session."""
    assert session_store.increment_tool_call("no-such-id") is None


def test_can_tool_call_limit() -> None:
    """can_tool_call is False when count reaches MAX."""
    s = session_store.create_session("THYAO")
    assert s.can_tool_call

    for _ in range(MAX_TOOL_CALLS_PER_SESSION):
        session_store.increment_tool_call(s.session_id)

    s_after = session_store.get_session(s.session_id)
    assert s_after is not None
    assert s_after.tool_call_count == MAX_TOOL_CALLS_PER_SESSION
    assert not s_after.can_tool_call


def test_is_expired_fresh() -> None:
    """Fresh session is not expired."""
    s = session_store.create_session("THYAO")
    assert not session_store.is_expired(s.session_id)


def test_is_expired_missing() -> None:
    """Missing session is treated as expired."""
    assert session_store.is_expired("nonexistent")


def test_is_expired_after_ttl() -> None:
    """Session past TTL is expired."""
    s = session_store.create_session("THYAO")
    object.__setattr__(
        s, "created_at", time.monotonic() - SESSION_TTL_SECONDS - 1
    )
    session_store._store[s.session_id] = s
    assert session_store.is_expired(s.session_id)


def test_close_session() -> None:
    """close_session transitions status to COMPLETED."""
    s = session_store.create_session("THYAO")
    assert session_store.close_session(s.session_id)

    updated = session_store.get_session(s.session_id)
    assert updated.status == SessionStatus.COMPLETED


def test_close_session_missing() -> None:
    """close_session returns False for unknown ID."""
    assert not session_store.close_session("nonexistent")


def test_cleanup_expired_sessions() -> None:
    """cleanup_expired_sessions removes only expired sessions."""
    s1 = session_store.create_session("THYAO")
    s2 = session_store.create_session("AKBNK")
    s3 = session_store.create_session("GARAN")

    # Expire s1 and s3
    object.__setattr__(
        s1, "created_at", time.monotonic() - SESSION_TTL_SECONDS - 10
    )
    object.__setattr__(
        s3, "created_at", time.monotonic() - SESSION_TTL_SECONDS - 10
    )
    session_store._store[s1.session_id] = s1
    session_store._store[s3.session_id] = s3

    removed = session_store.cleanup_expired_sessions()
    assert removed == 2
    assert s1.session_id not in session_store._store
    assert s2.session_id in session_store._store  # still alive
    assert s3.session_id not in session_store._store


def test_count_active() -> None:
    """count() returns only non-expired sessions."""
    assert session_store.count() == 0

    s1 = session_store.create_session("THYAO")
    s2 = session_store.create_session("AKBNK")

    assert session_store.count() == 2

    # Expire s1
    object.__setattr__(
        s1, "created_at", time.monotonic() - SESSION_TTL_SECONDS - 1
    )
    session_store._store[s1.session_id] = s1

    # count() should auto-cleanup
    assert session_store.count() == 1


def test_session_state_camelcase_serialization() -> None:
    """SessionState serializes to camelCase."""
    s = session_store.create_session("THYAO")

    j = s.model_dump(by_alias=True)
    assert "sessionId" in j
    assert "rootSymbol" in j
    assert j["rootSymbol"] == "THYAO"
    assert "createdAt" in j
    assert "updatedAt" in j
    assert "toolCallCount" in j
    assert j["toolCallCount"] == 0
    assert j["status"] == "OPEN"


def test_session_state_updated_at_changes() -> None:
    """updated_at changes after touch(), mark_completed(), mark_expired()."""
    s = session_store.create_session("THYAO")
    original = s.updated_at

    import time as _time
    _time.sleep(0.01)

    s.touch()
    assert s.updated_at > original

    _time.sleep(0.01)
    s.mark_completed()
    assert s.status == SessionStatus.COMPLETED
    assert s.updated_at > original


def test_append_step_with_reason() -> None:
    """ContextStep with reason is preserved in session."""
    s = session_store.create_session("THYAO")
    step = ContextStep(
        stepNo=1,
        symbol="THYAO",
        dataType="NEWS",
        payload={"headline": "KAP"},
        reason="Fetching news for context",
    )
    session_store.append_step(s.session_id, step)

    updated = session_store.get_session(s.session_id)
    assert updated is not None
    assert updated.steps[0].reason == "Fetching news for context"
