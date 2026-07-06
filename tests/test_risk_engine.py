"""Unit tests for RiskEngine."""

from __future__ import annotations

import pytest

from app.core.risk_config import RiskConfig
from app.models.signal import (
    EntryRange,
    OrderType,
    SignalAction,
    SignalMode,
    SignalRequest,
)
from app.services.risk_engine import RiskDecision, RiskEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(symbol: str = "THYAO", mode: SignalMode = SignalMode.MANUAL, **kwargs) -> SignalRequest:
    defaults: dict = dict(
        requestId="test-001",
        symbol=symbol,
        timeframe="1h",
        lastPrice=100.0,
        open=99.0,
        high=102.0,
        low=98.0,
        volume=1000.0,
        rsi=55.0,
        mode=mode,
    )
    defaults.update(kwargs)
    return SignalRequest(**defaults)


def _make_buy_decision(confidence: float = 85.0, qty: float = 5, **kwargs) -> RiskDecision:
    """BUY decision with required entryRange / stopLoss / targetPrice."""
    defaults: dict = dict(
        action=SignalAction.BUY,
        confidence=confidence,
        reason="Strong BUY",
        qty=qty,
        entry_range=EntryRange(min=95.0, max=102.0),
        stop_loss=93.0,
        target_price=110.0,
    )
    defaults.update(kwargs)
    return RiskDecision(**defaults)


def _cfg(**kwargs) -> RiskConfig:
    defaults: dict = dict(
        allowed_symbols="THYAO,AKBNK,SISE,KCHOL,TUPRS",
        locked_long_term_symbols="ASELS,EREGL",
        max_position_value_per_symbol=3000,
        max_daily_trade_count=3,
        min_confidence_for_buy=75,
        min_confidence_for_sell=70,
        allow_sell_long_term=False,
        allow_short_selling=False,
        disable_trading_after="23:59",
    )
    defaults.update(kwargs)
    return RiskConfig(**defaults, _env_file="")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAllowedSymbols:
    """Check 1: Symbol not in allowedSymbols → WAIT."""

    def test_allowed_symbol_goes_through(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", mode=SignalMode.LIVE)
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.BUY

    def test_disallowed_symbol_blocked(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="GARAN")
        dec = RiskDecision(action=SignalAction.BUY, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False
        assert "not in the allowed list" in resp.reason

    def test_case_insensitive_symbol_lookup(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="thyao", mode=SignalMode.LIVE)
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True


class TestLockedLongTerm:
    """Check 2: lockedLongTermSymbols block SELL."""

    def test_locked_symbol_sell_blocked(self):
        engine = RiskEngine(_cfg(allowed_symbols="ASELS,THYAO"))
        req = _make_request(symbol="ASELS", totalAccountQty=20, botPositionQty=10)
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False
        assert "locked long-term" in resp.reason.lower()

    def test_locked_symbol_sell_allowed_when_override(self):
        engine = RiskEngine(_cfg(allowed_symbols="ASELS,THYAO", allow_sell_long_term=True))
        req = _make_request(symbol="ASELS", totalAccountQty=20, botPositionQty=10, mode=SignalMode.LIVE)
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.SELL

    def test_locked_symbol_buy_goes_through(self):
        """BUY on locked symbol should not be blocked — lock is sell-only."""
        engine = RiskEngine(_cfg(allowed_symbols="ASELS,THYAO"))
        req = _make_request(symbol="ASELS", mode=SignalMode.LIVE)
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.BUY


class TestSellPositionChecks:
    """Check 3: SELL needs botPositionQty > 0."""

    def test_sell_with_zero_position_blocked(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", botPositionQty=0)
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False
        assert "no bot position" in resp.reason.lower()

    def test_sell_with_position_succeeds(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", totalAccountQty=20, botPositionQty=10, mode=SignalMode.LIVE)
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.SELL


class TestSellQtyClamp:
    """Check 4: SELL qty ≤ botPositionQty (bot kendi pozisyonu üstü satamaz)."""

    def test_sell_qty_exceeds_position_clamped(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", totalAccountQty=30, botPositionQty=10, mode=SignalMode.LIVE)
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=20)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.SELL
        assert resp.qty == 10.0
        assert resp.allow_order is True
        assert "clamped" in resp.reason.lower()


class TestLockedLongTermQty:
    """Check 5: lockedLongTermQty never sellable — uses totalAccountQty floor.

    Formula: sellableQty = min(botPositionQty, max(0, totalAccountQty - lockedLongTermQty))
    """

    def test_all_qty_locked_blocks_sell(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", totalAccountQty=10, botPositionQty=10, lockedLongTermQty=10)
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False
        assert "no sellable qty" in resp.reason.lower()

    def test_partial_locked_sellable_qty_capped(self):
        """totalAccountQty=8, lockedLongTermQty=3 → free=5, bot=10 → sellable=5."""
        engine = RiskEngine(_cfg())
        req = _make_request(
            symbol="THYAO",
            totalAccountQty=8,
            botPositionQty=10,
            lockedLongTermQty=3,
            mode=SignalMode.LIVE,
        )
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=10)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.SELL
        assert resp.qty == 5.0  # min(10, max(0, 8-3)) = 5
        assert resp.allow_order is True
        assert "clamped" in resp.reason.lower()

    def test_account_free_limits_bot_position(self):
        """totalAccountQty=120, locked=100 → free=20, bot=50 → sellable=20 (hesap tarafı sınırlar)."""
        engine = RiskEngine(_cfg())
        req = _make_request(
            symbol="THYAO",
            totalAccountQty=120,
            botPositionQty=50,
            lockedLongTermQty=100,
            mode=SignalMode.LIVE,
        )
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=40)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.SELL
        assert resp.qty == 20.0  # min(50, max(0, 120-100)) = 20
        assert resp.allow_order is True
        assert "clamped" in resp.reason.lower()

    def test_negative_account_free_floor_to_zero(self):
        """totalAccountQty=10, locked=20 → free=0, bot=5 → sellable=0 → BLOCKED."""
        engine = RiskEngine(_cfg())
        req = _make_request(
            symbol="THYAO",
            totalAccountQty=10,
            botPositionQty=5,
            lockedLongTermQty=20,
        )
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False
        assert "no sellable qty" in resp.reason.lower()

    def test_no_free_blocks_even_with_bot_position(self):
        """totalAccountQty=0 → free=0, bot=10 → sellable=0 → BLOCKED (güvenli taraf)."""
        engine = RiskEngine(_cfg())
        req = _make_request(
            symbol="THYAO",
            totalAccountQty=0,
            botPositionQty=10,
            lockedLongTermQty=0,
        )
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False
        assert "no sellable qty" in resp.reason.lower()


class TestPaperMode:
    """PAPER mode — never allowOrder, never requiresConfirmation."""

    def test_paper_mode_blocks_order(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", mode=SignalMode.PAPER)
        dec = _make_buy_decision(confidence=95.0)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.requires_confirmation is False
        assert "PAPER mode" in resp.reason

    def test_paper_mode_even_with_perfect_confidence(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", mode=SignalMode.PAPER)
        dec = _make_buy_decision(confidence=100.0)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.requires_confirmation is False

    def test_paper_mode_sell_also_blocked(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", mode=SignalMode.PAPER, totalAccountQty=20, botPositionQty=10)
        dec = RiskDecision(action=SignalAction.SELL, confidence=95.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.requires_confirmation is False
        assert "PAPER mode" in resp.reason


class TestManualMode:
    """MANUAL mode — never allowOrder, requiresConfirmation for BUY/SELL."""

    def test_manual_buy_requires_confirmation(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", mode=SignalMode.MANUAL)
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.requires_confirmation is True
        assert resp.action == SignalAction.BUY
        assert resp.qty > 0
        assert "requires user confirmation" in resp.reason

    def test_manual_wait_no_confirmation(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", mode=SignalMode.MANUAL)
        dec = RiskDecision(action=SignalAction.WAIT, confidence=90.0)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.requires_confirmation is False
        assert resp.action == SignalAction.WAIT

    def test_manual_sell_requires_confirmation(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", mode=SignalMode.MANUAL, totalAccountQty=20, botPositionQty=10)
        dec = RiskDecision(
            action=SignalAction.SELL,
            confidence=85.0,
            qty=5,
            entry_range=EntryRange(min=97, max=102),
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.requires_confirmation is True
        assert resp.action == SignalAction.SELL

    def test_manual_preserves_buy_details(self):
        """MANUAL keeps entryRange, stopLoss, targetPrice even though allowOrder=false."""
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", mode=SignalMode.MANUAL)
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.entry_range is not None
        assert resp.stop_loss is not None
        assert resp.target_price is not None
        assert resp.order_type == OrderType.LIMIT


class TestLiveMode:
    """LIVE mode — allowOrder when risk passes, never requiresConfirmation."""

    def test_live_mode_allows_order(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", mode=SignalMode.LIVE)
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.requires_confirmation is False

    def test_live_low_confidence_blocked(self):
        engine = RiskEngine(_cfg(min_confidence_for_buy=80))
        req = _make_request(symbol="THYAO", mode=SignalMode.LIVE)
        dec = _make_buy_decision(confidence=70.0)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.requires_confirmation is False
        assert "confidence" in resp.reason.lower()

    def test_live_wait_no_confirmation(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO", mode=SignalMode.LIVE)
        dec = RiskDecision(action=SignalAction.WAIT, confidence=90.0)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.requires_confirmation is False
        assert resp.action == SignalAction.WAIT


class TestConfidenceThreshold:
    """Check 7: Confidence below threshold → allowOrder=False."""

    def test_buy_below_threshold_blocked(self):
        engine = RiskEngine(_cfg(min_confidence_for_buy=75))
        req = _make_request(symbol="THYAO", mode=SignalMode.LIVE)
        dec = _make_buy_decision(confidence=70.0, stop_loss=None)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.action == SignalAction.WAIT
        assert "missing stopLoss" in resp.reason

    def test_buy_at_threshold_succeeds(self):
        engine = RiskEngine(_cfg(min_confidence_for_buy=75))
        req = _make_request(symbol="THYAO", mode=SignalMode.LIVE)
        dec = _make_buy_decision(confidence=75.0)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True

    def test_sell_below_threshold_blocked(self):
        engine = RiskEngine(_cfg(min_confidence_for_sell=70))
        req = _make_request(symbol="THYAO", totalAccountQty=20, botPositionQty=10, mode=SignalMode.LIVE)
        dec = RiskDecision(action=SignalAction.SELL, confidence=65.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False

    def test_sell_at_threshold_succeeds(self):
        engine = RiskEngine(_cfg(min_confidence_for_sell=70))
        req = _make_request(symbol="THYAO", totalAccountQty=20, botPositionQty=10, mode=SignalMode.LIVE)
        dec = RiskDecision(action=SignalAction.SELL, confidence=70.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True


class TestInvalidAction:
    """Check 8: Null/unknown action defaults to WAIT."""

    def test_null_decision_defaults_to_wait(self):
        engine = RiskEngine(_cfg())
        req = _make_request()
        resp = engine.evaluate(req)  # no decision
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False

    def test_wait_action_never_allows_order(self):
        engine = RiskEngine(_cfg())
        req = _make_request()
        dec = RiskDecision(action=SignalAction.WAIT, confidence=99.0)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False


class TestBuyPreflight:
    """Check 9: BUY in LIVE/MANUAL needs entryRange + stopLoss + targetPrice."""

    def test_buy_missing_entry_range_blocked(self):
        engine = RiskEngine(_cfg())
        req = _make_request(mode=SignalMode.LIVE)
        dec = _make_buy_decision(entry_range=None)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert "missing entryRange" in resp.reason

    def test_buy_missing_stop_loss_blocked(self):
        engine = RiskEngine(_cfg())
        req = _make_request(mode=SignalMode.LIVE)
        dec = _make_buy_decision(stop_loss=None)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert "missing stopLoss" in resp.reason

    def test_buy_missing_target_price_blocked(self):
        engine = RiskEngine(_cfg())
        req = _make_request(mode=SignalMode.LIVE)
        dec = _make_buy_decision(target_price=None)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert "missing targetPrice" in resp.reason

    def test_buy_all_params_present_succeeds(self):
        engine = RiskEngine(_cfg())
        req = _make_request(mode=SignalMode.LIVE)
        dec = _make_buy_decision()  # all three present
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.BUY

    def test_buy_missing_entry_range_waits(self):
        """Missing entryRange → action WAIT."""
        engine = RiskEngine(_cfg())
        req = _make_request(mode=SignalMode.LIVE)
        dec = RiskDecision(action=SignalAction.BUY, confidence=90.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.action == SignalAction.WAIT
        assert "missing entryRange" in resp.reason

    def test_buy_stop_loss_not_below_entry_min(self):
        """stopLoss >= entryRange.min → WAIT."""
        engine = RiskEngine(_cfg())
        req = _make_request(mode=SignalMode.LIVE)
        dec = _make_buy_decision(
            entry_range=EntryRange(min=95.0, max=102.0),
            stop_loss=96.0,  # above entry.min
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.action == SignalAction.WAIT
        assert "stopLoss must be below entryRange.min" in resp.reason

    def test_buy_stop_loss_equal_entry_min_blocked(self):
        """stopLoss == entryRange.min → WAIT."""
        engine = RiskEngine(_cfg())
        req = _make_request(mode=SignalMode.LIVE)
        dec = _make_buy_decision(
            entry_range=EntryRange(min=95.0, max=102.0),
            stop_loss=95.0,
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.action == SignalAction.WAIT
        assert "stopLoss must be below entryRange.min" in resp.reason

    def test_buy_target_price_not_above_entry_max(self):
        """targetPrice <= entryRange.max → WAIT."""
        engine = RiskEngine(_cfg())
        req = _make_request(mode=SignalMode.LIVE)
        dec = _make_buy_decision(
            entry_range=EntryRange(min=95.0, max=102.0),
            target_price=100.0,  # below entry.max
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.action == SignalAction.WAIT
        assert "targetPrice must be above entryRange.max" in resp.reason

    def test_buy_target_price_equal_entry_max_blocked(self):
        """targetPrice == entryRange.max → WAIT."""
        engine = RiskEngine(_cfg())
        req = _make_request(mode=SignalMode.LIVE)
        dec = _make_buy_decision(
            entry_range=EntryRange(min=95.0, max=102.0),
            target_price=102.0,
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.action == SignalAction.WAIT
        assert "targetPrice must be above entryRange.max" in resp.reason

    def test_buy_entry_min_gt_max_blocked(self):
        """entryRange.min > entryRange.max → WAIT."""
        engine = RiskEngine(_cfg())
        req = _make_request(mode=SignalMode.LIVE)
        dec = _make_buy_decision(
            entry_range=EntryRange(min=105.0, max=100.0),
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.action == SignalAction.WAIT
        assert "entryRange.min > entryRange.max" in resp.reason

    def test_buy_valid_range_passes_preflight(self):
        """Valid entryRange/stopLoss/targetPrice → risk kontrolleri devam eder."""
        engine = RiskEngine(_cfg())
        req = _make_request(mode=SignalMode.LIVE)
        dec = _make_buy_decision(
            entry_range=EntryRange(min=95.0, max=102.0),
            stop_loss=93.0,
            target_price=110.0,
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.BUY

    def test_buy_missing_all_three_fields(self):
        """All three fields missing → WAIT with all listed."""
        engine = RiskEngine(_cfg())
        req = _make_request(mode=SignalMode.LIVE)
        dec = RiskDecision(action=SignalAction.BUY, confidence=90.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.action == SignalAction.WAIT
        assert "missing entryRange, stopLoss, targetPrice" in resp.reason


class TestLimitOrderBehaviour:
    """LIVE/MANUAL modes produce LIMIT orders, not MARKET."""

    def test_buy_produces_limit_order(self):
        engine = RiskEngine(_cfg())
        req = _make_request(mode=SignalMode.LIVE)
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.order_type == OrderType.LIMIT

    def test_buy_price_capped_at_last_price(self):
        """entryRange.max (110) > lastPrice (100) → price = lastPrice."""
        engine = RiskEngine(_cfg())
        req = _make_request(mode=SignalMode.LIVE, lastPrice=100.0)
        dec = _make_buy_decision(
            entry_range=EntryRange(min=95.0, max=110.0),
            target_price=115.0,
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.price == 100.0  # min(110, 100)

    def test_buy_price_uses_entry_max_when_below_last(self):
        """entryRange.max (98) < lastPrice (100) → price = 98."""
        engine = RiskEngine(_cfg())
        req = _make_request(mode=SignalMode.LIVE, lastPrice=100.0)
        dec = _make_buy_decision(
            entry_range=EntryRange(min=92.0, max=98.0),
            stop_loss=88.0,
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.price == 98.0  # min(98, 100)

    def test_sell_produces_limit_order(self):
        engine = RiskEngine(_cfg())
        req = _make_request(mode=SignalMode.LIVE, symbol="THYAO", totalAccountQty=20, botPositionQty=10)
        dec = RiskDecision(
            action=SignalAction.SELL,
            confidence=85.0,
            reason="Take profit",
            qty=5,
            entry_range=EntryRange(min=97.0, max=100.0),
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.order_type == OrderType.LIMIT

    def test_sell_price_uses_entry_min_or_last(self):
        """entryRange.min (97) < lastPrice (100) → price = max(97, 100) = 100."""
        engine = RiskEngine(_cfg())
        req = _make_request(mode=SignalMode.LIVE, symbol="THYAO", totalAccountQty=20, botPositionQty=10, lastPrice=100.0)
        dec = RiskDecision(
            action=SignalAction.SELL,
            confidence=85.0,
            reason="Take profit",
            qty=5,
            entry_range=EntryRange(min=97.0, max=102.0),
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.price == 100.0  # max(97, 100)

    def test_sell_price_floors_at_last(self):
        """entryRange.min (103) > lastPrice (100) → price = 103 (floor)."""
        engine = RiskEngine(_cfg())
        req = _make_request(mode=SignalMode.LIVE, symbol="THYAO", totalAccountQty=20, botPositionQty=10, lastPrice=100.0)
        dec = RiskDecision(
            action=SignalAction.SELL,
            confidence=85.0,
            reason="Take profit",
            qty=5,
            entry_range=EntryRange(min=103.0, max=106.0),
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.price == 103.0  # max(103, 100)


class TestEntryRangeParsing:
    """_parse_entry_range handles camelCase + snake_case dicts."""

    def test_camel_case_nested(self):
        from app.routers.signal import _parse_entry_range
        result = _parse_entry_range({
            "entryRange": {"min": 100, "max": 105},
        })
        assert result is not None
        assert result.min == 100.0
        assert result.max == 105.0

    def test_snake_case_nested(self):
        from app.routers.signal import _parse_entry_range
        result = _parse_entry_range({
            "entry_range": {"min": 98, "max": 103},
        })
        assert result is not None
        assert result.min == 98.0
        assert result.max == 103.0

    def test_camel_case_flat(self):
        from app.routers.signal import _parse_entry_range
        result = _parse_entry_range({
            "entryMin": 95, "entryMax": 100,
        })
        assert result is not None
        assert result.min == 95.0
        assert result.max == 100.0

    def test_snake_case_flat(self):
        from app.routers.signal import _parse_entry_range
        result = _parse_entry_range({
            "entry_min": 97, "entry_max": 104,
        })
        assert result is not None
        assert result.min == 97.0
        assert result.max == 104.0

    def test_no_entry_range_returns_none(self):
        from app.routers.signal import _parse_entry_range
        result = _parse_entry_range({"action": "BUY"})
        assert result is None

    def test_garbage_entry_range_returns_none(self):
        """Garbage values in entryRange don't raise."""
        from app.routers.signal import _parse_entry_range
        assert _parse_entry_range({
            "entryRange": {"min": "n/a", "max": "???"},
        }) is None

    def test_entry_range_with_one_missing_returns_none(self):
        """If min present but max missing → None."""
        from app.routers.signal import _parse_entry_range
        assert _parse_entry_range({
            "entryRange": {"min": 95},
        }) is None

    def test_non_numeric_entry_range_returns_none(self):
        """String values that aren't numbers → None."""
        from app.routers.signal import _parse_entry_range
        assert _parse_entry_range({
            "entry_range": {"min": "high", "max": "low"},
        }) is None


class TestDailyTradeCount:
    """Check 4: dailyTradeCount ≥ maxDailyTradeCount → BUY/SELL blocked."""

    def test_buy_blocked_at_limit(self):
        """dailyTradeCount=3, maxDailyTradeCount=3 → BUY blocked."""
        engine = RiskEngine(_cfg(max_daily_trade_count=3))
        req = _make_request(symbol="THYAO", mode=SignalMode.LIVE, dailyTradeCount=3)
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.action == SignalAction.WAIT
        assert "daily trade count limit reached" in resp.reason.lower()
        assert "3/3" in resp.reason

    def test_buy_blocked_over_limit(self):
        """dailyTradeCount=5, maxDailyTradeCount=3 → BUY blocked."""
        engine = RiskEngine(_cfg(max_daily_trade_count=3))
        req = _make_request(symbol="THYAO", mode=SignalMode.LIVE, dailyTradeCount=5)
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.action == SignalAction.WAIT
        assert "daily trade count limit reached" in resp.reason.lower()

    def test_sell_blocked_at_limit(self):
        """dailyTradeCount=3, maxDailyTradeCount=3 → SELL blocked."""
        engine = RiskEngine(_cfg(max_daily_trade_count=3))
        req = _make_request(
            symbol="THYAO", mode=SignalMode.LIVE,
            totalAccountQty=20, botPositionQty=10, dailyTradeCount=3,
        )
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.action == SignalAction.WAIT
        assert "daily trade count limit reached" in resp.reason.lower()

    def test_buy_succeeds_below_limit(self):
        """dailyTradeCount=2, maxDailyTradeCount=3 → BUY risk kontrollerine devam eder."""
        engine = RiskEngine(_cfg(max_daily_trade_count=3))
        req = _make_request(symbol="THYAO", mode=SignalMode.LIVE, dailyTradeCount=2)
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.BUY

    def test_sell_succeeds_below_limit(self):
        """dailyTradeCount=2, maxDailyTradeCount=3 → SELL risk kontrollerine devam eder."""
        engine = RiskEngine(_cfg(max_daily_trade_count=3))
        req = _make_request(
            symbol="THYAO", mode=SignalMode.LIVE,
            totalAccountQty=20, botPositionQty=10, dailyTradeCount=2,
        )
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.SELL

    def test_wait_not_blocked_at_limit(self):
        """WAIT kararları dailyTradeCount'tan etkilenmez."""
        engine = RiskEngine(_cfg(max_daily_trade_count=3))
        req = _make_request(symbol="THYAO", mode=SignalMode.MANUAL, dailyTradeCount=5)
        dec = RiskDecision(action=SignalAction.WAIT, confidence=90.0)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False  # MANUAL always false
        # Reason should NOT mention daily trade count
        assert "daily trade count" not in resp.reason.lower()

    def test_default_daily_trade_count_zero(self):
        """dailyTradeCount belirtilmezse 0 kabul edilir → limiti aşmaz."""
        engine = RiskEngine(_cfg(max_daily_trade_count=3))
        req = _make_request(symbol="THYAO", mode=SignalMode.LIVE)  # dailyTradeCount defaults to 0
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True


class TestCutoffTime:
    """Check 3: cutoff sonrası BUY/SELL engellenir, WAIT etkilenmez."""

    def test_buy_blocked_after_cutoff(self, monkeypatch):
        """can_trade_now() returns False → BUY blocked."""
        from app.core.risk_config import RiskConfig

        monkeypatch.setattr(RiskConfig, "can_trade_now", lambda self, now=None: False)
        engine = RiskEngine(_cfg(disable_trading_after="17:30"))
        req = _make_request(symbol="THYAO", mode=SignalMode.LIVE)

        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.action == SignalAction.WAIT
        assert "after cutoff time 17:30" in resp.reason.lower()

    def test_sell_blocked_after_cutoff(self, monkeypatch):
        """can_trade_now() returns False → SELL blocked."""
        from app.core.risk_config import RiskConfig

        monkeypatch.setattr(RiskConfig, "can_trade_now", lambda self, now=None: False)
        engine = RiskEngine(_cfg(disable_trading_after="17:30"))
        req = _make_request(
            symbol="THYAO", mode=SignalMode.LIVE,
            totalAccountQty=20, botPositionQty=10,
        )

        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.action == SignalAction.WAIT
        assert "after cutoff time 17:30" in resp.reason.lower()

    def test_wait_not_blocked_after_cutoff(self, monkeypatch):
        """WAIT kararları cutoff'tan etkilenmez."""
        from app.core.risk_config import RiskConfig

        monkeypatch.setattr(RiskConfig, "can_trade_now", lambda self, now=None: False)
        engine = RiskEngine(_cfg(disable_trading_after="17:30"))
        req = _make_request(symbol="THYAO", mode=SignalMode.MANUAL)

        dec = RiskDecision(action=SignalAction.WAIT, confidence=90.0)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False  # MANUAL always false
        # Reason should NOT mention cutoff
        assert "cutoff" not in resp.reason.lower()


class TestMaxPositionValue:
    """BUY value can't exceed maxPositionValuePerSymbol."""

    def test_buy_exceeds_max_value_blocked(self):
        engine = RiskEngine(_cfg(max_position_value_per_symbol=500))
        req = _make_request(symbol="THYAO", lastPrice=100)  # qty*100 > 500
        dec = RiskDecision(action=SignalAction.BUY, confidence=85.0, qty=6)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False

    def test_buy_within_limit_succeeds(self):
        engine = RiskEngine(_cfg(max_position_value_per_symbol=500))
        req = _make_request(symbol="THYAO", lastPrice=100, mode=SignalMode.LIVE)
        dec = _make_buy_decision(confidence=85.0, qty=5)  # 5*100 = 500
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
