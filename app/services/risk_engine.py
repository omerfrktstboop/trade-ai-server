"""Risk engine — applies safety rules to every trading decision.

The ``RiskEngine`` sits between a raw AI / strategy decision and the final
``SignalResponse``. It guarantees that no dangerous order ever leaves the
server, regardless of the source decision.

Checks applied (in order):

1. **Unknown symbol** — not in ``allowed_symbols`` → WAIT
2. **Long-term lock** — locked symbol, SELL action → WAIT (protects ``ASELS``, ``EREGL``)
3. **Trading cutoff** — past ``disable_trading_after`` → BUY/SELL blocked
3.5. **Trade-profile mode permission** — REAL_LIVE/DEMO_LIVE not permitted by active profile → blocked
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
from decimal import ROUND_DOWN, Decimal
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
from app.services.effective_risk_config import EffectiveRiskConfig
from app.services.position_sizing import (
    PositionSizingResult,
    PositionSizingService,
    TradeSizingContext,
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
    qty: int = 0
    entry_range: Optional[EntryRange] = None
    stop_loss: Optional[Decimal] = None
    target_price: Optional[Decimal] = None


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

    def __init__(
        self,
        config: RiskConfig,
        effective_config: EffectiveRiskConfig | None = None,
    ) -> None:
        self.config = config
        self.effective_config = effective_config
        self.last_sizing_result: PositionSizingResult | None = None
        self.last_sizing_trade: TradeSizingContext | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        request: SignalRequest,
        decision: RiskDecision | None = None,
        market_regime: str | None = None,
    ) -> SignalResponse:
        """Run all risk checks and return a safe ``SignalResponse``.

        Checks (ordered):
        1.  Symbol — allowed list
        2.  Action — normalisation
        2.5 Macro regime filter — index DOWNTREND blocks BUY;
            HIGH_VOLATILITY tightens the confidence threshold (+15)
        3.  Long‑term locked symbols (SELL blocked unless override)
        4.  Cutoff time (BUY/SELL blocked after disable_trading_after)
        5.  Daily trade count (BUY/SELL blocked when dailyTradeCount >= maxDailyTradeCount)
        6.  Short selling guard (SELL needs botPositionQty > 0)
        7.  SELL qty clamp — sellableQty = min(botPositionQty, max(0, totalAccountQty − lockedLongTermQty))
        8.  Max position value (BUY)
        9.  Confidence threshold
        10. Mode gates (PAPER / MANUAL / LIVE / DEMO_LIVE / REAL_LIVE)
        11. BUY pre-flight (entryRange, stopLoss, targetPrice required)

        Args:
            market_regime: Endeks (XU100) makro rejimi — ``DOWNTREND`` /
                ``HIGH_VOLATILITY`` / diğerleri. ``None``/``UNKNOWN`` iken
                makro filtre uygulanmaz (fail-open: endeks verisi yok diye
                sistem durmaz). SELL hiçbir rejimde bloklanmaz.
        """
        if decision is None:
            decision = DEFAULT_WAIT
        self.last_sizing_result = None
        self.last_sizing_trade = None

        reasons: list[str] = []

        # Makro rejime göre güven eşiği sertleştirme (aşağıda 7. adımda
        # threshold'a eklenir).
        confidence_penalty = 0.0

        # ── 1. Unknown symbol ────────────────────────────────────────
        liquidation_sell = (
            decision.action == SignalAction.SELL and request.bot_position_qty > 0
        )
        if not self.config.is_symbol_allowed(request.symbol) and not liquidation_sell:
            return self._block(
                request,
                f"Symbol {request.symbol} is not in the allowed list",
            )

        # ── 2. Normalise action ──────────────────────────────────────
        action = decision.action
        if not isinstance(action, SignalAction):
            action = SignalAction.WAIT
            reasons.append("Unknown action — defaulting to WAIT")

        # ── 2.5. Macro regime filter (index-level) ───────────────────
        # Ayı piyasasında yeni pozisyon açılmaz; SELL (çıkış) her zaman
        # serbesttir. Yüksek volatilitede BUY tamamen bloklanmaz ama güven
        # eşiği sertleşir — sadece en net sinyaller geçer.
        regime = (market_regime or "").strip().upper()
        if regime == "DOWNTREND" and action == SignalAction.BUY:
            return self._block(
                request,
                "BUY blocked: market index is in DOWNTREND (bear regime) — "
                "no new long positions while the broad market falls",
            )
        if regime == "HIGH_VOLATILITY" and action == SignalAction.BUY:
            confidence_penalty = 15.0
            reasons.append(
                "Market index HIGH_VOLATILITY — BUY confidence threshold +15"
            )

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

        # ── 3.55. Trade-profile mode permission ──────────────────────
        # Authoritative gate for allow_real_live/allow_demo_live, regardless
        # of what mode the caller sent. The scanner and the Matriks gateway
        # each apply their own mode checks too, but this is the layer every
        # evaluation path passes through.
        if (
            request.mode == SignalMode.REAL_LIVE
            and not self.config.real_live_mode_allowed
        ):
            return self._block(
                request,
                "REAL_LIVE blocked: not permitted by active trade profile / botEnableRealOrders",
            )
        if (
            request.mode == SignalMode.DEMO_LIVE
            and not self.config.demo_live_mode_allowed
        ):
            return self._block(
                request,
                "DEMO_LIVE blocked: not permitted by active trade profile",
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
                0, request.total_account_qty - request.locked_long_term_qty
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
            # AI never selects SELL quantity either. Preserve the existing
            # ownership/locked-lot safety rule and deterministically exit only
            # the integer quantity the bot is allowed to sell.
            qty = int(Decimal(str(sellable_qty)).to_integral_value(rounding=ROUND_DOWN))
            if qty <= 0:
                return self._block(
                    request, "SELL blocked: sellable integer qty is zero"
                )
            reasons.append(
                f"SELL qty deterministically clamped to {qty} "
                f"(bot_pos={request.bot_position_qty}, free_acct={account_free_qty})"
            )

        if action == SignalAction.BUY and self.effective_config is not None:
            if request.account_sizing_context is None:
                return self._block(
                    request,
                    "BUY blocked: AccountSizingContext unavailable; TASK 1B adapter required",
                )
            if (
                decision.entry_range is None
                or decision.stop_loss is None
                or decision.target_price is None
            ):
                return self._block(
                    request,
                    "BUY blocked: deterministic sizing requires entryRange, stopLoss and targetPrice",
                )
            self.last_sizing_trade = TradeSizingContext(
                symbol=request.symbol,
                entry_price=decision.entry_range.max,
                stop_loss=decision.stop_loss,
                target_price=decision.target_price,
                confidence=Decimal(str(decision.confidence)),
                current_price=Decimal(str(request.last_price)),
            )
            self.last_sizing_result = PositionSizingService().calculate_buy_size(
                account=request.account_sizing_context,
                trade=self.last_sizing_trade,
                limits=self.effective_config,
            )
            if not self.last_sizing_result.allowed:
                return self._block(request, self.last_sizing_result.reason)
            qty = self.last_sizing_result.qty

        # ── 6. Max position value check ──────────────────────────────
        if action == SignalAction.BUY and qty > 0 and self.effective_config is None:
            current_qty = max(
                0.0,
                request.bot_position_qty,
                request.total_account_qty,
            )
            projected_qty = current_qty + qty
            projected_value = projected_qty * request.last_price
            if projected_value > self.config.max_position_value_per_symbol:
                reasons.append(
                    f"Projected BUY value {projected_value:.0f} > max "
                    f"{self.config.max_position_value_per_symbol:.0f}"
                )
                return self._block(
                    request,
                    f"BUY blocked: projected position {projected_qty:g} shares / "
                    f"{projected_value:.0f} value exceeds max "
                    f"{self.config.max_position_value_per_symbol:.0f}",
                )

        # ── 7. Confidence threshold ──────────────────────────────────
        technical_block_reason = self._technical_feature_block_reason(request, action)
        if technical_block_reason:
            return self._block(request, technical_block_reason)

        if action in (SignalAction.BUY, SignalAction.SELL):
            threshold = (
                self.config.get_min_confidence(action.value) + confidence_penalty
            )
            confidence_ok = decision.confidence >= threshold
            if not confidence_ok:
                reasons.append(
                    f"Confidence {decision.confidence:.1f} < threshold {threshold:.0f}"
                )
        else:
            # WAIT (or any non-BUY/SELL action) has no meaningful confidence
            # gate — get_min_confidence()'s 100.0 fallback is for unknown
            # action values, not a real threshold, and allow_order is forced
            # false for WAIT regardless (see mode gates below). Attaching
            # "Confidence X < threshold 100" to a WAIT reason was misleading:
            # it implied a runaway 100%-confidence requirement that doesn't
            # exist in any trade profile.
            confidence_ok = True

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
            if (
                request.quote_age_seconds is not None
                and request.quote_age_seconds
                > self.config.max_quote_age_seconds_for_buy
            ):
                return f"BUY blocked: quote age {request.quote_age_seconds:.1f}s exceeds max {self.config.max_quote_age_seconds_for_buy:.1f}s"
            if request.depth_reliable:
                if (
                    request.depth_age_seconds is not None
                    and request.depth_age_seconds
                    > self.config.max_depth_age_seconds_for_buy
                ):
                    return f"BUY blocked: depth age {request.depth_age_seconds:.1f}s exceeds max {self.config.max_depth_age_seconds_for_buy:.1f}s"
                if (
                    request.spread_pct is not None
                    and request.spread_pct > self.config.max_spread_pct_for_buy
                ):
                    return f"BUY blocked: spread {request.spread_pct:.2f}% exceeds max {self.config.max_spread_pct_for_buy:.2f}%"
                if (
                    request.depth_bid_ask_ratio_top10 is not None
                    and request.depth_bid_ask_ratio_top10
                    < self.config.min_depth_bid_ask_ratio_top10_for_buy
                ):
                    return (
                        "BUY blocked: depth bid/ask ratio Top10 below profile minimum"
                    )
                if (
                    request.depth_sell_pressure_score is not None
                    and request.depth_sell_pressure_score
                    > self.config.max_depth_sell_pressure_score_for_buy
                ):
                    return "BUY blocked: depth sell pressure exceeds profile maximum"
                if (
                    self.config.block_buy_on_strong_sell_pressure
                    and request.depth_order_book_signal == "STRONG_SELL_PRESSURE"
                ):
                    return "BUY blocked: STRONG_SELL_PRESSURE in reliable depth"
                if (
                    self.config.block_buy_on_near_ask_wall
                    and request.depth_nearest_ask_wall_distance_pct is not None
                    and abs(request.depth_nearest_ask_wall_distance_pct)
                    <= self.config.near_wall_distance_pct
                ):
                    return "BUY blocked: concentrated ask wall is too close"
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
        return (signal == SignalAction.BUY.value and action == SignalAction.SELL) or (
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
