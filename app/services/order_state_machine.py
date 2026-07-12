"""Single monotonic order lifecycle transition authority."""

from __future__ import annotations

FINAL = {"FILLED", "REJECTED", "CANCELED", "CANCELLED", "EXPIRED"}
RANK = {"RESERVED": 1, "SEND_IN_PROGRESS": 5, "SENT_PENDING": 10, "SEND_UNKNOWN": 15, "NEW": 20, "CANCEL_REQUESTED": 30, "PARTIALLY_FILLED": 40, "ERROR_RECONCILIATION_REQUIRED": 90, "FILLED": 100, "REJECTED": 100, "CANCELED": 100, "CANCELLED": 100, "EXPIRED": 100}


def transition(current: str | None, incoming: str, *, current_filled: float = 0, incoming_filled: float = 0) -> tuple[bool, str]:
    old, new = (current or "RESERVED").upper(), incoming.upper()
    if old in FINAL and new != old:
        return False, "conflicting final event"
    if RANK.get(new, 0) < RANK.get(old, 0):
        return False, "status regression"
    if incoming_filled < current_filled:
        return False, "filled quantity regression"
    return True, ""
