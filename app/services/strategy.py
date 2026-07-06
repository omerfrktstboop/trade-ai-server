"""Dummy strategy — generates a simple technical signal from a request.

This is a placeholder. Real strategies will be plugged in later.
The rules are deliberately simple and bullish-biased so the RiskEngine
can demonstrate overrides (PAPER mode, confidence thresholds, etc.).
"""

from __future__ import annotations

from app.core.risk_config import RiskConfig
from app.models.signal import (
    EntryRange,
    SignalAction,
    SignalRequest,
)
from app.services.risk_engine import RiskDecision


# ── Public API ────────────────────────────────────────────────────────────────


def generate_dummy_decision(
    request: SignalRequest,
    config: RiskConfig | None = None,
) -> RiskDecision:
    """Produce a raw trading decision from simple technical rules.

    Rules (priority order):

    1. **BUY** when ``RSI < 35`` **and** ``lastPrice > ema20``
       — signals oversold bounce confirmed by price above short-term MA.
    2. **SELL** when ``botPositionQty > 0`` **and** ``RSI > 75``
       — signals overbought with existing position to exit.
    3. **WAIT** in all other cases.

    Parameters:
        request: The inbound signal request with OHLC + indicator values.
        config:  Optional risk config (for future context-aware strategies).

    Returns:
        A RiskDecision ready to be validated by RiskEngine.
    """
    rsi = request.rsi
    ema20 = request.ema20
    last_price = request.last_price
    bot_qty = request.bot_position_qty

    # Normalise: if indicator fields are None, treat as neutral
    rsi_val = float(rsi) if rsi is not None else 50.0
    ema20_val = float(ema20) if ema20 is not None else last_price

    # ── BUY: oversold + price above EMA ──────────────────────────────────
    if rsi_val < 35 and last_price > ema20_val:
        qty = _suggest_qty(request, config)
        confidence = _map_rsi_confidence(rsi_val, side="buy")
        return RiskDecision(
            action=SignalAction.BUY,
            confidence=confidence,
            reason="Dummy BUY: RSI oversold (< 35) + price > EMA20",
            qty=qty,
        )

    # ── SELL: overbought + has position ──────────────────────────────────
    if bot_qty > 0 and rsi_val > 75:
        qty = min(bot_qty, _suggest_qty(request, config))
        confidence = _map_rsi_confidence(rsi_val, side="sell")
        return RiskDecision(
            action=SignalAction.SELL,
            confidence=confidence,
            reason=f"Dummy SELL: RSI overbought (> 75) + holding {bot_qty} shares",
            qty=qty,
        )

    # ── WAIT ─────────────────────────────────────────────────────────────
    return RiskDecision(
        action=SignalAction.WAIT,
        reason=f"Dummy WAIT: RSI={rsi_val:.0f}, no trigger met",
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _suggest_qty(request: SignalRequest, config: RiskConfig | None = None) -> float:
    """Derive a sensible position size from the last price and config limits.

    Falls back to 1 share if no price/limit information is available.
    """
    if request.last_price and request.last_price > 0:
        max_value = (
            config.max_position_value_per_symbol
            if config
            else 1000.0
        )
        # Target ~20 % of max value per symbol
        target = max_value * 0.20
        qty = max(1, int(target / request.last_price))
        return float(qty)
    return 1.0


def _map_rsi_confidence(rsi: float, side: str) -> float:
    """Map RSI extreme level to a confidence score.

    Closer to 0 / 100 → higher confidence. Capped at 95 so the RiskEngine
    still has a meaningful threshold to check against.
    """
    if side == "buy":
        # RSI 0  → confidence 95
        # RSI 20 → confidence 75
        # RSI 35 → confidence 60
        return max(60.0, min(95.0, 95.0 - rsi))
    # Side == sell
    # RSI 75  → confidence 60
    # RSI 80  → confidence 68
    # RSI 90  → confidence 84
    # RSI 100 → confidence 95
    return max(60.0, min(95.0, 60.0 + (rsi - 75.0) * 1.4))
