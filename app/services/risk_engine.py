"""Risk engine — applies safety rules to every trading decision.

The ``RiskEngine`` sits between a raw AI / strategy decision and the final
``SignalResponse``. It guarantees that no dangerous order ever leaves the
server, regardless of the source decision.

Checks applied (in order):

1. **Unknown symbol** — not in ``allowed_symbols`` → WAIT
2. **Long-term lock** — locked symbol, SELL action → WAIT (protects ``ASELS``, ``EREGL``)
3. **Trading cutoff** — past ``disable_trading_after`` → BUY/SELL blocked
4. **Daily trade count** — ``dailyTradeCount ≥ maxDailyTradeCount`` → BUY/SELL blocked
5. **Short selling** — SELL when ``botPositionQty == 0`` → WAIT
6. **Over-sell** — SELL qty > ``botPositionQty`` → cap qty
7. **Locked qty** — ``lockedLongTermQty`` deducted from available SELL qty
8. **Max position value** — BUY value > ``maxPositionValuePerSymbol`` → WAIT
9. **Confidence floor** — below ``minConfidence`` threshold → ``allowOrder=False``
10. **Mode-based allowOrder / requiresConfirmation** — PAPER/MANUAL/LIVE/DEMO_LIVE/REAL_LIVE rules
11. **BUY pre-flight** — missing ``entryRange`` / ``stopLoss`` / ``targetPrice`` → blocked
"""

from __future__ import annotations

from dataclasses import dataclass
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

DEFAULT_WAIT = RiskDecision(
    action=SignalAction.WAIT, reason="Safe default: no AI decision yet."
)


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
        """Run all risk checks and return a safe ``SignalResponse``.

        Checks (ordered):
        1.  Symbol — allowed list
        2.  Action — normalisation
        3.  Long‑term locked symbols (SELL blocked unless override)
        4.  Cutoff time (BUY/SELL blocked after disable_trading_after)
        5.  Daily trade count (BUY/SELL blocked when dailyTradeCount >= maxDailyTradeCount)
        6.  Short selling guard (SELL needs botPositionQty > 0)
        7.  SELL qty clamp — sellableQty = min(botPositionQty, max(0, totalAccountQty − lockedLongTermQty))
        8.  Max position value (BUY)
        9.  Confidence threshold
        10. Mode gates (PAPER / MANUAL / LIVE / DEMO_LIVE / REAL_LIVE)
        11. BUY pre-flight (entryRange, stopLoss, targetPrice required)
        """
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
        if action == SignalAction.SELL and self.config.is_long_term_locked(
            request.symbol
        ):
            if not self.config.allow_sell_long_term:
                return self._block(
                    request,
                    f"SELL blocked: {request.symbol} is a locked long-term symbol",
                )

        # ── 3.5. Trading cutoff time ─────────────────────────────────
        if (
            action in (SignalAction.BUY, SignalAction.SELL)
            and not self.config.can_trade_now()
        ):
            return self._block(
                request,
                f"Trading blocked: after cutoff time {self.config.disable_trading_after}",
            )

        # ── 3.6. Daily trade count limit ─────────────────────────────
        if (
            action in (SignalAction.BUY, SignalAction.SELL)
            and request.daily_trade_count >= self.config.max_daily_trade_count
        ):
            return self._block(
                request,
                f"Trading blocked: daily trade count limit reached "
                f"({request.daily_trade_count}/{self.config.max_daily_trade_count})",
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
        #   sellableQty = min(botPositionQty, max(0, totalAccountQty - lockedLongTermQty))
        #
        #   Bot yalnızca kendi aldığı lotu satabilir (botPositionQty).
        #   Uzun vadeli kilitli lot hesap bakiye üzerinden korunur.
        #   İki sınırlayıcıdan küçük olan geçerlidir.
        qty = decision.qty
        if action == SignalAction.SELL:
            account_free_qty = max(
                0.0, request.total_account_qty - request.locked_long_term_qty
            )
            sellable_qty = min(request.bot_position_qty, account_free_qty)

            if sellable_qty <= 0:
                return self._block(
                    request,
                    f"SELL blocked: no sellable qty "
                    f"(bot={request.bot_position_qty}, "
                    f"free_acct={account_free_qty}, "
                    f"locked={request.locked_long_term_qty})",
                )
            if qty > sellable_qty:
                reasons.append(
                    f"SELL qty clamped from {qty} to {sellable_qty} "
                    f"(bot_pos={request.bot_position_qty}, "
                    f"free_acct={account_free_qty})"
                )
                qty = sellable_qty

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
        technical_block_reason = self._technical_feature_block_reason(request, action)
        if technical_block_reason:
            return self._block(request, technical_block_reason)

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
            requires_confirmation = action != SignalAction.WAIT
            if requires_confirmation:
                reasons.append("MANUAL mode — requires user confirmation")
            else:
                reasons.append("MANUAL mode — allowOrder forced to false")
        else:  # LIVE / DEMO_LIVE / REAL_LIVE
            requires_confirmation = False
            allow_order = confidence_ok and action != SignalAction.WAIT
            if request.mode == SignalMode.DEMO_LIVE and allow_order:
                reasons.append("DEMO_LIVE mode — demo order may be sent by client")
            elif request.mode == SignalMode.REAL_LIVE and allow_order:
                reasons.append("REAL_LIVE mode — client-side real order gate required")

        # ── 9. BUY pre-flight: entryRange / stopLoss / targetPrice ──
        if action == SignalAction.BUY and request.mode in (
            SignalMode.MANUAL,
            SignalMode.LIVE,
            SignalMode.DEMO_LIVE,
            SignalMode.REAL_LIVE,
        ):
            missing: list[str] = []
            if decision.entry_range is None:
                missing.append("entryRange")
            if decision.stop_loss is None:
                missing.append("stopLoss")
            if decision.target_price is None:
                missing.append("targetPrice")

            if missing:
                reason_text = f"BUY blocked: missing {', '.join(missing)}"
                return self._block(
                    request,
                    reason_text,
                )

            # Validate price relationships
            entry = decision.entry_range
            sl = decision.stop_loss
            tp = decision.target_price

            if entry.min > entry.max:  # type: ignore[union-attr]  # guarded above
                return self._block(
                    request,
                    "BUY blocked: entryRange.min > entryRange.max",
                )

            if sl is not None and sl >= entry.min:  # type: ignore[union-attr]
                return self._block(
                    request,
                    "BUY blocked: stopLoss must be below entryRange.min",
                )

            if tp is not None and tp <= entry.max:  # type: ignore[union-attr]
                return self._block(
                    request,
                    "BUY blocked: targetPrice must be above entryRange.max",
                )

        # ── Determine order type and price ──────────────────────────
        show_details = allow_order or request.mode == SignalMode.MANUAL

        if request.mode == SignalMode.PAPER:
            order_type = OrderType.NONE
            price = None
        elif show_details:
            order_type = OrderType.LIMIT
            if action == SignalAction.BUY:
                price = (
                    decision.entry_range.max
                )  # BUY pre-flight guarantees entry_range is set
            elif action == SignalAction.SELL:
                price = request.last_price
            else:
                price = None

            # Safety: if price cannot be determined for BUY/SELL, block
            if action in (SignalAction.BUY, SignalAction.SELL) and price is None:
                return self._block(
                    request,
                    f"{action.value} blocked: cannot determine limit price",
                )
        else:
            order_type = OrderType.NONE
            price = None

        # ── Build final reason ───────────────────────────────────────
        # Important: when the AI provider returns an error (e.g. API 401,
        # network timeout, parse failure) the decision.reason carries that
        # real error message.  We MUST preserve it so Matriks can display
        # the actual cause, not just the generic confidence-threshold note.
        if reasons:
            reason_parts = reasons.copy()
            if decision.reason and decision.reason not in "; ".join(reasons):
                reason_parts.insert(0, decision.reason)
            base_reason = "; ".join(reason_parts)
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

    def _technical_feature_block_reason(
        self, request: SignalRequest, action: SignalAction
    ) -> str | None:
        """Return a block reason when optional Matriks features flag danger."""
        if action not in (SignalAction.BUY, SignalAction.SELL):
            return None

        if action == SignalAction.BUY:
            natr = request.natr
            if natr is not None and natr > self.config.max_natr_for_buy:
                return (
                    f"BUY blocked: nATR {natr:.2f}% exceeds max "
                    f"{self.config.max_natr_for_buy:.2f}%"
                )

            depth_drop = request.depth_queue_drop_pct
            if (
                depth_drop is not None
                and depth_drop > self.config.max_depth_queue_drop_pct_for_buy
            ):
                return (
                    f"BUY blocked: bid queue dropped {depth_drop:.1f}% "
                    f"(max {self.config.max_depth_queue_drop_pct_for_buy:.1f}%)"
                )

        if self.config.require_alpha_trend_alignment:
            alpha_signal = self._normalise_signal(request.alpha_trend_signal)
            if self._opposes_action(alpha_signal, action):
                return (
                    f"{action.value} blocked: alphaTrendSignal={alpha_signal} "
                    f"opposes action"
                )

        if self.config.require_indicator_consensus_alignment:
            consensus = self._normalise_signal(request.indicator_consensus)
            if self._opposes_action(consensus, action):
                count = (
                    request.indicator_sell_count
                    if consensus == SignalAction.SELL.value
                    else request.indicator_buy_count
                )
                if count is None or count >= self.config.min_indicator_consensus_count:
                    return (
                        f"{action.value} blocked: indicatorConsensus={consensus} "
                        f"opposes action"
                    )

        return None

    @staticmethod
    def _normalise_signal(raw_value: str | None) -> str | None:
        if not raw_value:
            return None
        value = str(raw_value).strip().upper()
        if value in {"BUY", "LONG", "AL"}:
            return SignalAction.BUY.value
        if value in {"SELL", "SHORT", "SAT"}:
            return SignalAction.SELL.value
        if value in {"WAIT", "NEUTRAL", "HOLD", "NONE"}:
            return SignalAction.WAIT.value
        return None

    @staticmethod
    def _opposes_action(signal: str | None, action: SignalAction) -> bool:
        return (
            signal == SignalAction.BUY.value and action == SignalAction.SELL
        ) or (
            signal == SignalAction.SELL.value and action == SignalAction.BUY
        )

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
