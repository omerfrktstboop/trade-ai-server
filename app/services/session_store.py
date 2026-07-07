"""Session management — in-memory store with TTL.

Development store using dict. Production should use Redis/DB.
Provides SessionState (Pydantic model) and SessionStore (thread-safe dict).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from app.models.signal import ContextStep

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

SESSION_TTL_SECONDS: int = 300  # 5 minutes
MAX_TOOL_CALLS_PER_SESSION: int = 3


# ── SessionStatus ────────────────────────────────────────────────────────────


class SessionStatus(str, Enum):
    """Lifecycle states for a session."""

    OPEN = "OPEN"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"


# ── SessionState ─────────────────────────────────────────────────────────────


class SessionState(BaseModel):
    """Stateful session tracking for agentic multi-turn evaluation.

    Accumulates context steps across turns and enforces limits.
    """

    session_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex, alias="sessionId"
    )
    root_symbol: str = Field(..., alias="rootSymbol")
    created_at: float = Field(default_factory=time.monotonic, alias="createdAt")
    updated_at: float = Field(default_factory=time.monotonic, alias="updatedAt")
    steps: list[ContextStep] = Field(default_factory=list)
    tool_call_count: int = Field(0, alias="toolCallCount")
    status: SessionStatus = Field(SessionStatus.OPEN)

    model_config = {"populate_by_name": True}

    # ── Derived properties ────────────────────────────────────────────────

    @property
    def age_seconds(self) -> float:
        """Seconds since session creation."""
        return time.monotonic() - self.created_at

    @property
    def is_expired(self) -> bool:
        """True when age exceeds TTL."""
        return self.age_seconds >= SESSION_TTL_SECONDS

    @property
    def can_tool_call(self) -> bool:
        """True when tool call limit not yet reached."""
        return self.tool_call_count < MAX_TOOL_CALLS_PER_SESSION

    # ── Mutators ──────────────────────────────────────────────────────────

    def touch(self) -> None:
        """Update the ``updated_at`` timestamp."""
        object.__setattr__(self, "updated_at", time.monotonic())

    def mark_completed(self) -> None:
        """Transition status to COMPLETED."""
        object.__setattr__(self, "status", SessionStatus.COMPLETED)
        self.touch()

    def mark_expired(self) -> None:
        """Transition status to EXPIRED."""
        object.__setattr__(self, "status", SessionStatus.EXPIRED)
        self.touch()


# ── SessionStore ─────────────────────────────────────────────────────────────


class SessionStore:
    """Thread-safe in-memory session store with TTL cleanup.

    Usage::

        store = SessionStore()
        session = store.create_session("THYAO")
        ...
        store.append_step(session.session_id, step)
        store.increment_tool_call(session.session_id)
        if store.is_expired(session.session_id):
            # return WAIT
    """

    def __init__(self) -> None:
        self._store: dict[str, SessionState] = {}
        self._lock = threading.RLock()

    # ── Public API ────────────────────────────────────────────────────────

    def create_session(self, root_symbol: str) -> SessionState:
        """Create a brand-new session and store it.

        Args:
            root_symbol: The symbol being evaluated (e.g. "THYAO").
        """
        session = SessionState(root_symbol=root_symbol)
        with self._lock:
            self._store[session.session_id] = session
        logger.debug(
            "Created session %s for %s", session.session_id, root_symbol
        )
        return session

    def get_session(self, session_id: str) -> Optional[SessionState]:
        """Get a live session by ID.

        Returns:
            SessionState if found and not expired, else None.
            Expired sessions are automatically removed.
        """
        with self._lock:
            session = self._store.get(session_id)
            if session is None:
                return None
            if session.is_expired:
                session.mark_expired()
                self._store.pop(session_id, None)
                logger.debug("Session %s expired and removed", session_id)
                return None
            return session

    def append_step(
        self, session_id: str, step: ContextStep
    ) -> Optional[SessionState]:
        """Append a context step to the session.

        Returns updated session or None if session is missing/expired.
        """
        session = self.get_session(session_id)
        if session is None:
            return None
        with self._lock:
            session.steps.append(step)
            session.touch()
            self._store[session_id] = session
        logger.debug("Appended step %d to session %s", step.step_no, session_id)
        return session

    def increment_tool_call(
        self, session_id: str
    ) -> Optional[SessionState]:
        """Increment tool call counter.

        Returns updated session or None if session is missing/expired.
        """
        session = self.get_session(session_id)
        if session is None:
            return None
        with self._lock:
            object.__setattr__(
                session, "tool_call_count", session.tool_call_count + 1
            )
            session.touch()
            self._store[session_id] = session
        logger.debug(
            "Tool call %d/%d for session %s",
            session.tool_call_count,
            MAX_TOOL_CALLS_PER_SESSION,
            session_id,
        )
        return session

    def is_expired(self, session_id: str) -> bool:
        """Check if a session is expired (or missing).

        True means the caller should treat this as WAIT.
        """
        session = self.get_session(session_id)
        if session is None:
            return True
        return session.is_expired

    def close_session(self, session_id: str) -> bool:
        """Close a session (mark COMPLETED).

        Returns True if the session was found and closed, False otherwise.
        """
        with self._lock:
            session = self._store.get(session_id)
            if session is None:
                return False
            session.mark_completed()
            self._store[session_id] = session
            logger.debug("Session %s closed", session_id)
            return True

    def cleanup_expired_sessions(self) -> int:
        """Remove all expired sessions from the store.

        Returns the count of removed sessions.
        """
        with self._lock:
            expired_ids = [
                sid
                for sid, s in self._store.items()
                if s.age_seconds >= SESSION_TTL_SECONDS
            ]
            for sid in expired_ids:
                self._store[sid].mark_expired()
                self._store.pop(sid, None)
            if expired_ids:
                logger.debug(
                    "Cleaned up %d expired sessions", len(expired_ids)
                )
            return len(expired_ids)

    def count(self) -> int:
        """Return the number of active (non-expired) sessions."""
        with self._lock:
            self.cleanup_expired_sessions()
            return len(self._store)


# ── Global singleton ─────────────────────────────────────────────────────────

session_store = SessionStore()
