"""Agent planner — decides next action for agentic multi-turn sessions.

Uses SessionState (v2) and AgenticDataType (v2).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.risk_config import risk_config
from app.models.signal import (
    AgentAction,
    AgenticDataType,
    FetchData,
)
from app.services.session_store import MAX_TOOL_CALLS_PER_SESSION, SessionState

# ── Required data checks ────────────────────────────────────────────────────

# The planner requests data types in order. tool_call_count acts as an index.
_CHECK_ORDER: list[tuple[AgenticDataType, str]] = [
    (AgenticDataType.DEPTH, "Derinlik verisi gerekli"),
    (AgenticDataType.AKD, "AKD (Açığa Kısa Dönüşüm) verisi gerekli"),
    (AgenticDataType.OHLCV, "OHLCV fiyat verisi gerekli"),
    (AgenticDataType.TECHNICAL, "Teknik indikatör verisi gerekli"),
    (AgenticDataType.NEWS, "Haber/KAP verisi gerekli"),
    (AgenticDataType.FUND, "Fon dağılımı gerekli"),
    (AgenticDataType.BROKER_FLOW, "Broker işlem akışı gerekli"),
]


# ── Plan result ──────────────────────────────────────────────────────────────


@dataclass
class PlanResult:
    """Outcome of the planner — either PROCEED (final) or a data request."""

    action: AgentAction
    fetch_data: FetchData | None = None
    reason: str = ""
    required_data_type: AgenticDataType | None = None
    proceed_to_ai: bool = False  # True → route to AI + RiskEngine


# ── Planner: v2 (SessionState-based) ─────────────────────────────────────────


def plan_next(session: SessionState) -> PlanResult:
    """Determine the next step for the given session.

    Logic:
    1. Symbol not allowed → WAIT
    2. Can still request data (tool_call_count < MAX) → FETCH_DATA
    3. Budget exhausted → PROCEED (delegate to AI/RiskEngine)
    """
    # ── Symbol check ──────────────────────────────────────────────────
    if not risk_config.is_symbol_allowed(session.root_symbol):
        return PlanResult(
            action=AgentAction.WAIT,
            reason=f"Symbol {session.root_symbol} is not in the allowed list",
        )

    # ── Can we still request more data? ───────────────────────────────
    if session.can_tool_call:
        idx = session.tool_call_count
        if idx < len(_CHECK_ORDER):
            data_type, reason = _CHECK_ORDER[idx]
            return PlanResult(
                action=AgentAction.FETCH_DATA,
                fetch_data=FetchData(
                    targetSymbol=session.root_symbol,
                    dataType=data_type,
                    reason=reason,
                ),
                reason=reason,
                required_data_type=data_type,
            )
        # All data types exhausted but budget remains → proceed to final
        return PlanResult(
            action=AgentAction.WAIT,
            reason="All data checks complete — delegating to AI for final decision",
            proceed_to_ai=True,
        )

    # ── Budget exhausted → final ──────────────────────────────────────
    return PlanResult(
        action=AgentAction.WAIT,
        reason="Tool call budget exhausted — delegating to AI for final decision",
        proceed_to_ai=True,
    )
