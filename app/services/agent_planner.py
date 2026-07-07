"""Agent planner — decides next action for agentic multi-turn sessions.

Uses SessionState (v2) and AgenticAction / AgenticDataType (canonical).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.risk_config import risk_config
from app.models.signal import (
    AgenticAction,
    AgenticDataType,
)
from app.services.session_store import MAX_TOOL_CALLS_PER_SESSION, SessionState

# ── Related symbols mapping ──────────────────────────────────────────────────
# When evaluating these root symbols, the planner first requests DEPTH data
# for the related symbol before collecting data for the root symbol itself.
RELATED_SYMBOLS: dict[str, str] = {
    "ANELE": "THYAO",
    "PGSUS": "THYAO",
    "TUPRS": "KCHOL",
}

# ── Data type request priority for same-symbol collection ────────────────────
# Requested in order when the root symbol has no related mapping (or after
# the related symbol's DEPTH has been collected).
_DATA_PRIORITY: list[tuple[AgenticDataType, str]] = [
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

    action: AgenticAction
    target_symbol: str | None = None
    required_data_type: AgenticDataType | None = None
    reason: str = ""
    proceed_to_ai: bool = False  # True → route to AI + RiskEngine


# ── Helpers ──────────────────────────────────────────────────────────────────


def _collected(session: SessionState) -> set[tuple[str, AgenticDataType]]:
    """Return the set of (symbol, dataType) pairs already in session.steps."""
    return {(step.symbol.upper(), step.data_type) for step in session.steps}


def _request_related_depth(
    session: SessionState, collected: set[tuple[str, AgenticDataType]]
) -> PlanResult | None:
    """If root symbol has a related symbol and DEPTH not yet collected, request it."""
    root_upper = session.root_symbol.upper()
    related = RELATED_SYMBOLS.get(root_upper)
    if related is None:
        return None

    key = (related, AgenticDataType.DEPTH)
    if key in collected:
        return None

    return PlanResult(
        action=AgenticAction.FETCH_DATA,
        target_symbol=related,
        required_data_type=AgenticDataType.DEPTH,
        reason=f"{root_upper} için {related} derinlik verisi gerekli (ilişkili hisse)",
    )


def _request_next_same_symbol(
    session: SessionState, collected: set[tuple[str, AgenticDataType]]
) -> PlanResult | None:
    """Request the next missing data type for the root symbol."""
    root_upper = session.root_symbol.upper()

    for data_type, reason in _DATA_PRIORITY:
        key = (root_upper, data_type)
        if key not in collected:
            return PlanResult(
                action=AgenticAction.FETCH_DATA,
                target_symbol=root_upper,
                required_data_type=data_type,
                reason=reason,
            )
    return None


# ── Planner: v2 (SessionState-based) ─────────────────────────────────────────


def plan_next(session: SessionState) -> PlanResult:
    """Determine the next step for the given session.

    Logic:
    1. Symbol not allowed → WAIT
    2. Check related symbols mapping — request related DEPTH first
    3. Request missing data types for root symbol
    4. All collected or budget exhausted → PROCEED (AI/RiskEngine)
    """
    # ── Symbol check ──────────────────────────────────────────────────
    if not risk_config.is_symbol_allowed(session.root_symbol):
        return PlanResult(
            action=AgenticAction.WAIT,
            reason=f"Symbol {session.root_symbol} is not in the allowed list",
        )

    # ── Already collected data types ───────────────────────────────────
    collected = _collected(session)

    # ── Can we still request more data? ───────────────────────────────
    if session.can_tool_call:
        # 1) Related symbol DEPTH (first priority)
        related_plan = _request_related_depth(session, collected)
        if related_plan is not None:
            return related_plan

        # 2) Next missing data type for root symbol
        same_plan = _request_next_same_symbol(session, collected)
        if same_plan is not None:
            return same_plan

        # All data types collected → proceed to AI
        return PlanResult(
            action=AgenticAction.WAIT,
            reason="All data checks complete — delegating to AI for final decision",
            proceed_to_ai=True,
        )

    # ── Budget exhausted → final ──────────────────────────────────────
    return PlanResult(
        action=AgenticAction.WAIT,
        reason="Tool call budget exhausted — delegating to AI for final decision",
        proceed_to_ai=True,
    )
