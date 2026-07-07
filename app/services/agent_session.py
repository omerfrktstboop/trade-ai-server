"""Agent session manager — in-memory state with TTL.

Each session tracks the context accumulated across multiple turns
and enforces safety limits (max tool calls, TTL expiry).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from app.models.signal import AgentAction, DataRequestType, FetchData

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

SESSION_TTL_SECONDS: int = 300  # 5 minutes
MAX_TOOL_CALLS_PER_SESSION: int = 3

# ── AgentSession ─────────────────────────────────────────────────────────────


@dataclass
class AgentSession:
    """Tracks a single multi-turn agent session.

    Accumulates context across turns and enforces tool-call limits.
    """

    session_id: str  # raw UUID (not the store key)
    symbol: str
    mode: str

    context_data: dict[str, object] = field(default_factory=dict)
    tool_calls: int = 0
    created_at: float = field(default_factory=time.monotonic)

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.created_at

    @property
    def is_expired(self) -> bool:
        return self.age_seconds >= SESSION_TTL_SECONDS

    @property
    def can_tool_call(self) -> bool:
        return self.tool_calls < MAX_TOOL_CALLS_PER_SESSION

    def add_context(self, key: str, value: object) -> None:
        """Merge context data — nested dicts are deep-merged."""
        existing = self.context_data.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged = {**existing, **value}
            self.context_data[key] = merged
        else:
            self.context_data[key] = value


# ── Session Store ────────────────────────────────────────────────────────────


class AgentSessionStore:
    """Thread-safe in-memory session store with TTL cleanup."""

    def __init__(self) -> None:
        self._store: OrderedDict[str, AgentSession] = OrderedDict()
        self._lock = threading.Lock()

    def _key(self, session_id: str, symbol: str) -> str:
        return f"{session_id}:{symbol}"

    # ── Public API ────────────────────────────────────────────────────────

    def create(self, session_id: str, symbol: str, mode: str) -> AgentSession:
        """Create a brand-new session (replaces any existing one with same key)."""
        session = AgentSession(session_id=session_id, symbol=symbol, mode=mode)
        key = self._key(session_id, symbol)
        with self._lock:
            self._store[key] = session
        return session

    def get(self, session_id: str, symbol: str) -> AgentSession | None:
        """Return live session or None if missing/expired."""
        key = self._key(session_id, symbol)
        with self._lock:
            self._cleanup_expired()
            session = self._store.get(key)
            if session is not None and session.is_expired:
                self._store.pop(key, None)
                return None
            return session

    def get_or_create(
        self, session_id: str, symbol: str, mode: str
    ) -> tuple[AgentSession, bool]:
        """Get existing session or create a new one.

        Returns (session, was_expired_or_missing).
        was_expired_or_missing is True when the previous session expired (or none existed).
        """
        import uuid

        # Generate a real session ID if none provided
        if not session_id:
            session_id = uuid.uuid4().hex

        existing = self.get(session_id, symbol)
        if existing is not None:
            return existing, False
        return self.create(session_id, symbol, mode), True

    def update(self, session: AgentSession) -> None:
        """Re-insert session to update order and reset TTL access tracking."""
        key = self._key(session.session_id, session.symbol)
        with self._lock:
            self._store[key] = session
            self._store.move_to_end(key)

    def remove(self, session_id: str, symbol: str) -> None:
        key = self._key(session_id, symbol)
        with self._lock:
            self._store.pop(key, None)

    def count(self) -> int:
        with self._lock:
            self._cleanup_expired()
            return len(self._store)

    def _cleanup_expired(self) -> None:
        """Remove all expired sessions. Must be called under lock."""
        expired = [
            k for k, s in self._store.items() if s.age_seconds >= SESSION_TTL_SECONDS
        ]
        for k in expired:
            self._store.pop(k, None)
        if expired:
            logger.debug("Cleaned up %d expired sessions", len(expired))


# ── Global singleton ─────────────────────────────────────────────────────────

agent_session_store = AgentSessionStore()
