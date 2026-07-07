"""Agent planner — decides next action in a multi-turn session.

The planner looks at accumulated context and determines whether
the agent has enough data to produce a BUY/SELL/WAIT decision,
or if it should request more data via FETCH_DATA.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.risk_config import risk_config
from app.models.signal import AgentAction, DataRequestType, FetchData
from app.services.agent_session import MAX_TOOL_CALLS_PER_SESSION, AgentSession

# ── Required data checks per symbol ──────────────────────────────────────────

# The planner requests data types in order. The tool_calls counter acts as
# an index into this list: call 1 gets first type, call 2 gets second, etc.
_CHECK_ORDER: list[tuple[DataRequestType, str]] = [
    (DataRequestType.INTRADAY_OHLC, "Detailed intraday OHLC needed"),
    (DataRequestType.VOLUME_DISTRIBUTION, "Volume distribution profile needed"),
    (DataRequestType.ORDER_FLOW, "Order flow / order book needed"),
    (DataRequestType.FUND_FLOW, "Fund flow context needed"),
    (DataRequestType.NEWS_DETAIL, "News detail context needed"),
]


# ── Plan result ───────────────────────────────────────────────────────────────


@dataclass
class PlanResult:
    """Outcome of the planner — either final or a data request."""

    action: AgentAction
    fetch_data: FetchData | None = None
    reason: str = ""


# ── Planner ───────────────────────────────────────────────────────────────────


def plan_next_action(session: AgentSession) -> PlanResult:
    """Determine the next step for the given agent session.

    Logic:
    1. Symbol not allowed → WAIT
    2. Can still request data (tool_calls < MAX) → FETCH_DATA
    3. Budget exhausted → final (delegate to AI/RiskEngine)
    """
    # Safety: check symbol is allowed
    if not risk_config.is_symbol_allowed(session.symbol):
        return PlanResult(
            action=AgentAction.WAIT,
            reason=f"Symbol {session.symbol} is not in the allowed list",
        )

    # ── Can we still request more data? ─────────────────────────
    if session.can_tool_call:
        # tool_calls tracks how many FETCH_DATA responses have been issued.
        # Use it as an index into the check order.
        idx = session.tool_calls
        if idx < len(_CHECK_ORDER):
            data_type, reason = _CHECK_ORDER[idx]
            return PlanResult(
                action=AgentAction.FETCH_DATA,
                fetch_data=FetchData(
                    targetSymbol=session.symbol,
                    dataType=data_type,
                    reason=reason,
                ),
                reason=reason,
            )
        # All data types exhausted but budget remains → go to final
        return PlanResult(
            action=AgentAction.WAIT,
            reason="All data checks complete — delegating to AI for final decision",
        )

    # ── Budget exhausted → final ─────────────────────────────────
    return PlanResult(
        action=AgentAction.WAIT,
        reason="Tool call budget exhausted — delegating to AI for final decision",
    )
