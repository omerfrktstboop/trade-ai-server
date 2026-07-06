"""Risk engine — applies safety rules to every trading decision.

The ``RiskEngine`` sits between a raw AI / strategy decision and the final
``SignalResponse``. It guarantees that no dangerous order ever leaves the
server, regardless of the source decision.

Checks applied (in order):

1. **Unknown symbol** — not in ``allowed_symbols`` → WAIT
2. **Long-term lock** — locked symbol, SELL action → WAIT (protects ``ASELS``, ``EREGL``)
3. **Empty position** — SELL when ``botPositionQty == 0`` → WAIT
4. **Over-sell** — SELL qty > ``botPositionQty`` → cap qty
5. **Locked qty** — ``lockedLongTermQty`` deducted from available SELL qty
6. **PAPER mode** — always ``allowOrder=False``
7. **Confidence floor** — below ``minConfidence` threshold → ``allowOrder=False``
8. **Invalid action** — unknown action string → WAIT
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.core.risk_config import RiskConfig
from app.models.signal import (
    EntryRange,
    OrderType,
    SignalAction,
    SignalMode,
    SignalRequest,
    SignalResponse,
)


# ---------------------------------------------------------------------------
# Intermediate decision object (before risk checks)
# ---------------------------------------------------------------------------


@dataclass
class RiskDecision:
    """Raw decision produced by AI or strategy before safety checks."""

    action: SignalAction = SignalAction.WAIT
    confidence: float = 0.0
    risk_score: float = 0.0
    reason: str = ""
    qty: float = 0.0
    entry_range: Optional[EntryRange] = None
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None


# ---------------------------------------------------------------------------
# Default decisions
# ---------------------------------------------------------------------------

DEFAULT_WAIT = RiskDecision(action=SignalAction.WAIT, reason="Safe default: no AI decision yet.")


# ---------------------------------------------------------------------------
# RiskEngine
# ---------------------------------------------------------------------------


class RiskEngine:
    """Applies all risk rules and produces a safety-guaranteed ``SignalResponse``.

    Usage::

        engine = RiskEngine(risk_config)
        decision = RiskDecision(action=SignalAction.BUY, confidence=82.0, qty=10)
        response = engine.evaluate(signal_request, decision)
    """

    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        request: SignalRequest,
        decision: RiskDecision | None = None,
    ) -> SignalResponse:
        """Run all risk checks and return a safe ``SignalResponse``."""
        if decision is None:
            decision = DEFAULT_WAIT

        reasons: list[str] = []

        # ── 1. Unknown symbol ────────────────────────────────────────
        if not self.config.is_symbol_allowed(request.symbol):
            return self._block(
                request,
                f"Symbol {request.symbol} is not in the allowed list",
            )

        # ── 2. Normalise action ──────────────────────────────────────
        action = decision.action
        if not isinstance(action, SignalAction):
            action = SignalAction.WAIT
            reasons.append("Unknown action — defaulting to WAIT")

        # ── 3. Long-term lock blocks SELL ────────────────────────────
        if action == SignalAction.SELL and self.config.is_long_term_locked(request.symbol):
            if not self.config.allow_sell_long_term:
                return self._block(
                    request,
                    f"SELL blocked: {request.symbol} is a locked long-term symbol",
                )

        # ── 4. Short selling not allowed ─────────────────────────────
        if not self.config.allow_short_selling and request.bot_position_qty <= 0:
            if action == SignalAction.SELL:
                return self._block(
                    request,
                    f"SELL blocked: no bot position to sell (qty={request.bot_position_qty})",
                )
            if action == SignalAction.BUY and request.bot_position_qty < 0:
                # BUY to close short: block since short selling is disabled
                pass  # not relevant right now but gate is here

        # ── 5. SELL qty clamp ────────────────────────────────────────
        qty = decision.qty
        if action == SignalAction.SELL:
            available = request.bot_position_qty - request.locked_long_term_qty
            if available <= 0:
                return self._block(
                    request,
                    f"SELL blocked: all held qty is locked long-term "
                    f"(bot={request.bot_position_qty}, locked={request.locked_long_term_qty})",
                )
            if qty > available:
                reasons.append(f"SELL qty clamped from {qty} to {available} (locked qty excluded)")
                qty = available

        # ── 6. Max position value check ──────────────────────────────
        if action == SignalAction.BUY and qty > 0:
            position_value = qty * request.last_price
            if position_value > self.config.max_position_value_per_symbol:
                reasons.append(
                    f"BUY value {position_value:.0f} > max {self.config.max_position_value_per_symbol:.0f}"
                )
                return self._block(
                    request,
                    f"BUY blocked: position value {position_value:.0f} exceeds max "
                    f"{self.config.max_position_value_per_symbol:.0f}",
                )

        # ── 7. Confidence threshold ──────────────────────────────────
        threshold = self.config.get_min_confidence(action.value)
        confidence_ok = decision.confidence >= threshold

        if not confidence_ok:
            reasons.append(
                f"Confidence {decision.confidence:.1f} < threshold {threshold:.0f}"
            )

        # ── 8. Mode-based allowOrder / requiresConfirmation ─────────
        if request.mode == SignalMode.PAPER:
            allow_order = False
            requires_confirmation = False
            reasons.append("PAPER mode — allowOrder forced to false")
        elif request.mode == SignalMode.MANUAL:
            allow_order = False
            requires_confirmation = (action != SignalAction.WAIT)
            if requires_confirmation:
                reasons.append("MANUAL mode — requires user confirmation")
            else:
                reasons.append("MANUAL mode — allowOrder forced to false")
        else:  # LIVE
            requires_confirmation = False
            allow_order = confidence_ok and action != SignalAction.WAIT

        # ── 9. BUY pre-flight: entryRange / stopLoss / targetPrice ──
        if (
            action == SignalAction.BUY
            and request.mode in (SignalMode.MANUAL, SignalMode.LIVE)
        ):
            if decision.entry_range is None:
                if allow_order:  # only LIVE can be True here
                    allow_order = False
                reasons.append("BUY missing entryRange")
            elif decision.stop_loss is None:
                if allow_order:
                    allow_order = False
                reasons.append("BUY missing stopLoss")
            elif decision.target_price is None:
                if allow_order:
                    allow_order = False
                reasons.append("BUY missing targetPrice")

        # ── Determine order type and price ──────────────────────────
        show_details = allow_order or request.mode == SignalMode.MANUAL

        if request.mode == SignalMode.PAPER:
            order_type = OrderType.NONE
            price = None
        elif show_details:
            order_type = OrderType.LIMIT
            if decision.entry_range is not None:
                if action == SignalAction.BUY:
                    price = min(decision.entry_range.max, request.last_price)
                elif action == SignalAction.SELL:
                    price = max(decision.entry_range.min, request.last_price)
                else:
                    price = request.last_price
            else:
                price = request.last_price
        else:
            order_type = OrderType.NONE
            price = None

        # ── Build final reason ───────────────────────────────────────
        if reasons:
            base_reason = "; ".join(reasons)
        elif decision.reason:
            base_reason = decision.reason
        else:
            base_reason = "Risk checks passed"

        return SignalResponse(
            requestId=request.request_id,
            symbol=request.symbol,
            action=action,
            qty=qty if show_details else 0.0,
            orderType=order_type,
            price=price,
            confidenceScore=decision.confidence,
            riskScore=decision.risk_score,
            allowOrder=allow_order,
            requiresConfirmation=requires_confirmation,
            reason=base_reason,
            entryRange=decision.entry_range if show_details else None,
            stopLoss=decision.stop_loss if show_details else None,
            targetPrice=decision.target_price if show_details else None,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _block(self, request: SignalRequest, reason: str) -> SignalResponse:
        """Return a WAIT / allowOrder=False response with the given reason."""
        return SignalResponse(
            requestId=request.request_id,
            symbol=request.symbol,
            action=SignalAction.WAIT,
            qty=0.0,
            orderType=OrderType.NONE,
            price=None,
            confidenceScore=0.0,
            riskScore=0.0,
            allowOrder=False,
            requiresConfirmation=False,
            reason=reason,
            entryRange=None,
            stopLoss=None,
            targetPrice=None,
        )
