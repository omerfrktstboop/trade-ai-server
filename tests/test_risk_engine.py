"""Unit tests for RiskEngine."""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.risk_config import RiskConfig
from app.models.signal import (
    EntryRange,
    OrderType,
    SignalAction,
    SignalRequest,
)
from app.services.risk_engine import RiskDecision, RiskEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(symbol: str = "THYAO", **kwargs) -> SignalRequest:
    # v2: mode kaldırıldı; kalan `mode=` kwarg'ları yok sayılır.
    kwargs.pop("mode", None)
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
        tradeEligible=True,
    )
    defaults.update(kwargs)
    if "totalAccountQty" in kwargs and "accountAvailableQty" not in kwargs:
        defaults["accountAvailableQty"] = kwargs["totalAccountQty"]
    return SignalRequest(**defaults)


def _make_buy_decision(
    confidence: float = 85.0, qty: float = 5, **kwargs
) -> RiskDecision:
    """BUY decision with required entryRange / stopLoss / targetPrice."""
    defaults: dict = dict(
        action=SignalAction.BUY,
        confidence=confidence,
        reason="Strong BUY",
        qty=qty,
        entry_range=EntryRange(min=95.0, max=102.0),
        stop_loss=93.0,
        target_price=116.0,
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
        timezone="Etc/GMT+12",
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
        req = _make_request(symbol="THYAO")
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.BUY

    def test_disallowed_symbol_research_only(self):
        """İzin dışı sembol: analiz korunur, emir yolu kapalı kalır."""
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="GARAN")
        dec = RiskDecision(action=SignalAction.BUY, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.BUY
        assert resp.confidence_score == 85.0
        assert resp.allow_order is False
        assert resp.qty == 0
        assert resp.requires_confirmation is False
        assert "not in the allowed order list" in resp.reason

    def test_case_insensitive_symbol_lookup(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="thyao")
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True


class TestEmptyAllowListAllowsAll:
    """allowedSymbols boş → tüm semboller emir evrenine dahil."""

    def test_empty_allowed_symbols_permits_any_symbol(self):
        engine = RiskEngine(_cfg(allowed_symbols=""))
        req = _make_request(symbol="GARAN")
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.BUY

    def test_nonempty_allowed_symbols_still_whitelists(self):
        engine = RiskEngine(_cfg(allowed_symbols="THYAO"))
        req = _make_request(symbol="GARAN")
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert "not in the allowed order list" in resp.reason

    def test_empty_allowed_symbols_does_not_promote_research_candidate(self):
        engine = RiskEngine(_cfg(allowed_symbols=""))
        req = _make_request(symbol="GARAN", tradeEligible=False)
        resp = engine.evaluate(req, _make_buy_decision())
        assert resp.allow_order is False
        assert resp.qty == 0
        assert "not in the active Trade Watchlist" in resp.reason


class TestDeclineSymbols:
    """declineSymbols kara listesi: BUY yasak, çıkış (SELL) serbest."""

    def test_declined_symbol_blocks_buy_even_when_allow_all(self):
        engine = RiskEngine(_cfg(allowed_symbols="", decline_symbols="GARAN"))
        req = _make_request(symbol="GARAN")
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.confidence_score == 85.0  # analiz korunur
        assert "decline blacklist" in resp.reason

    def test_declined_symbol_sell_exit_still_allowed(self):
        engine = RiskEngine(_cfg(allowed_symbols="", decline_symbols="THYAO"))
        req = _make_request(
            symbol="THYAO",
            totalAccountQty=20,
            accountAvailableQty=20,
            botPositionQty=10,
            
        )
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.SELL
        assert resp.allow_order is True
        assert "decline blacklist" not in resp.reason


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
        engine = RiskEngine(
            _cfg(allowed_symbols="ASELS,THYAO", allow_sell_long_term=True)
        )
        req = _make_request(
            symbol="ASELS",
            totalAccountQty=20,
            accountAvailableQty=20,
            botPositionQty=10,
            
        )
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.SELL

    def test_locked_symbol_buy_goes_through(self):
        """BUY on locked symbol should not be blocked — lock is sell-only."""
        engine = RiskEngine(_cfg(allowed_symbols="ASELS,THYAO"))
        req = _make_request(symbol="ASELS")
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
        req = _make_request(
            symbol="THYAO", totalAccountQty=20, botPositionQty=10
        )
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.SELL


class TestSellQtyClamp:
    """Check 4: SELL qty ≤ botPositionQty (bot kendi pozisyonu üstü satamaz)."""

    def test_sell_qty_exceeds_position_clamped(self):
        engine = RiskEngine(_cfg())
        req = _make_request(
            symbol="THYAO", totalAccountQty=30, botPositionQty=10
        )
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
        req = _make_request(
            symbol="THYAO", totalAccountQty=10, botPositionQty=10, lockedLongTermQty=10
        )
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


# v2: TestPaperMode / TestManualMode kaldırıldı. PAPER/MANUAL çalışma modları
# yok; allow_order artık yalnızca risk geçişini gösterir (dispatch systemMode
# ile scanner'da kapılanır), requires_confirmation her zaman False.


class TestRiskPassAllowsOrder:
    """v2: risk geçen BUY/SELL için allow_order=True, requires_confirmation=False."""

    def test_valid_buy_allows_order_without_confirmation(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO")
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.requires_confirmation is False
        assert resp.action == SignalAction.BUY

    def test_wait_never_allows_order(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO")
        dec = RiskDecision(action=SignalAction.WAIT, confidence=90.0)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.requires_confirmation is False
        assert resp.action == SignalAction.WAIT

    def test_valid_buy_preserves_order_details(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO")
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.entry_range is not None
        assert resp.stop_loss is not None
        assert resp.target_price is not None
        assert resp.order_type == OrderType.LIMIT


class TestLiveMode:
    """v2: risk geçince allow_order=True, requires_confirmation asla True olmaz."""

    def test_live_mode_allows_order(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO")
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.requires_confirmation is False

    def test_live_low_confidence_blocked(self):
        engine = RiskEngine(_cfg(min_confidence_for_buy=80))
        req = _make_request(symbol="THYAO")
        dec = _make_buy_decision(confidence=70.0)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.requires_confirmation is False
        assert "confidence" in resp.reason.lower()

    def test_live_wait_no_confirmation(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO")
        dec = RiskDecision(action=SignalAction.WAIT, confidence=90.0)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.requires_confirmation is False
        assert resp.action == SignalAction.WAIT

    def test_risk_pass_produces_limit_order(self):
        engine = RiskEngine(_cfg())
        req = _make_request(symbol="THYAO")
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.requires_confirmation is False
        assert resp.order_type == OrderType.LIMIT

    def test_low_confidence_blocked_no_order_type(self):
        engine = RiskEngine(_cfg(min_confidence_for_buy=90))
        req = _make_request(symbol="THYAO")
        dec = _make_buy_decision(confidence=80.0)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.requires_confirmation is False
        assert resp.order_type == OrderType.NONE
        assert "Confidence" in resp.reason


class TestConfidenceThreshold:
    """Check 7: Confidence below threshold → allowOrder=False."""

    def test_buy_below_threshold_blocked(self):
        engine = RiskEngine(_cfg(min_confidence_for_buy=75))
        req = _make_request(symbol="THYAO")
        dec = _make_buy_decision(confidence=70.0, stop_loss=None)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.action == SignalAction.WAIT
        assert "missing stopLoss" in resp.reason

    def test_buy_at_threshold_succeeds(self):
        engine = RiskEngine(_cfg(min_confidence_for_buy=75))
        req = _make_request(symbol="THYAO")
        dec = _make_buy_decision(confidence=75.0)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True

    def test_sell_below_threshold_blocked(self):
        engine = RiskEngine(_cfg(min_confidence_for_sell=70))
        req = _make_request(
            symbol="THYAO", totalAccountQty=20, botPositionQty=10
        )
        dec = RiskDecision(action=SignalAction.SELL, confidence=65.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False

    def test_sell_at_threshold_succeeds(self):
        engine = RiskEngine(_cfg(min_confidence_for_sell=70))
        req = _make_request(
            symbol="THYAO", totalAccountQty=20, botPositionQty=10
        )
        dec = RiskDecision(action=SignalAction.SELL, confidence=70.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True

    def test_wait_decision_does_not_get_misleading_threshold_100_note(self):
        """A WAIT decision has no meaningful confidence gate — get_min_confidence's
        100.0 fallback (for unrecognized action values) must not leak into the
        reason text as if a real 100%-confidence threshold were configured."""
        engine = RiskEngine(_cfg(min_confidence_for_buy=75, min_confidence_for_sell=70))
        req = _make_request()
        dec = RiskDecision(
            action=SignalAction.WAIT, confidence=20.0, reason="No clear signal"
        )
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False
        assert "threshold" not in resp.reason
        assert resp.reason == "No clear signal"


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
        req = _make_request()
        dec = _make_buy_decision(entry_range=None)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert "missing entryRange" in resp.reason

    def test_buy_missing_stop_loss_blocked(self):
        engine = RiskEngine(_cfg())
        req = _make_request()
        dec = _make_buy_decision(stop_loss=None)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert "missing stopLoss" in resp.reason

    def test_buy_missing_target_price_blocked(self):
        engine = RiskEngine(_cfg())
        req = _make_request()
        dec = _make_buy_decision(target_price=None)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert "missing targetPrice" in resp.reason

    def test_buy_all_params_present_succeeds(self):
        engine = RiskEngine(_cfg())
        req = _make_request()
        dec = _make_buy_decision()  # all three present
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.BUY

    def test_buy_missing_entry_range_waits(self):
        """Missing entryRange → action WAIT."""
        engine = RiskEngine(_cfg())
        req = _make_request()
        dec = RiskDecision(action=SignalAction.BUY, confidence=90.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.action == SignalAction.WAIT
        assert "missing entryRange" in resp.reason

    def test_buy_stop_loss_not_below_entry_min(self):
        """stopLoss >= entryRange.min → WAIT."""
        engine = RiskEngine(_cfg())
        req = _make_request()
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
        req = _make_request()
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
        req = _make_request()
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
        req = _make_request()
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
        req = _make_request()
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
        req = _make_request()
        dec = _make_buy_decision(
            entry_range=EntryRange(min=95.0, max=102.0),
            stop_loss=93.0,
            target_price=116.0,
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.BUY

    def test_buy_missing_all_three_fields(self):
        """All three fields missing → WAIT with all listed."""
        engine = RiskEngine(_cfg())
        req = _make_request()
        dec = RiskDecision(action=SignalAction.BUY, confidence=90.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.action == SignalAction.WAIT
        assert "missing entryRange, stopLoss, targetPrice" in resp.reason

    def test_buy_symmetric_reward_risk_blocked(self):
        """R/G < 1.5 (ör. 1:1 setup) → BUY bloklanır."""
        engine = RiskEngine(_cfg())
        req = _make_request()
        dec = _make_buy_decision(
            entry_range=EntryRange(min=95.0, max=102.0),
            stop_loss=93.0,  # risk = 9
            target_price=110.0,  # reward = 8 → R/G ≈ 0.89
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.action == SignalAction.WAIT
        assert "reward/risk" in resp.reason

    def test_buy_exact_minimum_reward_risk_passes(self):
        """R/G tam 1.5 → sınır dahil, geçer."""
        engine = RiskEngine(_cfg())
        req = _make_request()
        dec = _make_buy_decision(
            entry_range=EntryRange(min=95.0, max=102.0),
            stop_loss=94.0,  # risk = 8
            target_price=114.0,  # reward = 12 → R/G = 1.5
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.BUY


class TestLimitOrderBehaviour:
    """LIVE/MANUAL modes produce LIMIT orders, not MARKET."""

    def test_buy_produces_limit_order(self):
        engine = RiskEngine(_cfg())
        req = _make_request()
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.order_type == OrderType.LIMIT

    def test_buy_price_uses_entry_max(self):
        """BUY limit price = entryRange.max (110.0), cap uygulanmaz."""
        engine = RiskEngine(_cfg())
        req = _make_request(lastPrice=100.0)
        dec = _make_buy_decision(
            entry_range=EntryRange(min=95.0, max=110.0),
            target_price=136.0,
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.order_type == OrderType.LIMIT
        assert resp.price == 110.0

    def test_buy_price_uses_entry_max_when_below_last(self):
        """entryRange.max (98) < lastPrice (100) → price = 98.0."""
        engine = RiskEngine(_cfg())
        req = _make_request(lastPrice=100.0)
        dec = _make_buy_decision(
            entry_range=EntryRange(min=92.0, max=98.0),
            stop_loss=88.0,
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.order_type == OrderType.LIMIT
        assert resp.price == 98.0

    def test_sell_produces_limit_order(self):
        engine = RiskEngine(_cfg())
        req = _make_request(
            symbol="THYAO", totalAccountQty=20, botPositionQty=10
        )
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

    def test_sell_price_is_last_price(self):
        """SELL limit price = request.lastPrice always."""
        engine = RiskEngine(_cfg())
        req = _make_request(
            
            symbol="THYAO",
            totalAccountQty=20,
            botPositionQty=10,
            lastPrice=100.0,
        )
        dec = RiskDecision(
            action=SignalAction.SELL,
            confidence=85.0,
            reason="Take profit",
            qty=5,
            entry_range=EntryRange(min=97.0, max=102.0),
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.order_type == OrderType.LIMIT
        assert resp.price == 100.0

    def test_sell_price_uses_last_price(self):
        """SELL limit price = request.lastPrice (100.0), floor uygulanmaz."""
        engine = RiskEngine(_cfg())
        req = _make_request(
            
            symbol="THYAO",
            totalAccountQty=20,
            botPositionQty=10,
            lastPrice=100.0,
        )
        dec = RiskDecision(
            action=SignalAction.SELL,
            confidence=85.0,
            reason="Take profit",
            qty=5,
            entry_range=EntryRange(min=103.0, max=106.0),
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.order_type == OrderType.LIMIT
        assert resp.price == 100.0  # always lastPrice for SELL

    def test_market_order_never_produced(self):
        """Hiçbir senaryoda orderType=MARKET dönmemeli."""
        engine = RiskEngine(_cfg())

        scenarios = [
            # (name, request, decision)
            ("LIVE BUY", _make_request(), _make_buy_decision()),
            ("PAPER BUY", _make_request(), _make_buy_decision()),
            (
                "WAIT decision",
                _make_request(),
                RiskDecision(action=SignalAction.WAIT),
            ),
            ("_block path", _make_request(symbol="GARAN"), _make_buy_decision()),
        ]

        for name, req, dec in scenarios:
            resp = engine.evaluate(req, dec)
            assert resp.order_type != OrderType.MARKET, f"{name}: produced MARKET order"
            assert resp.order_type in (OrderType.LIMIT, OrderType.NONE), (
                f"{name}: unexpected orderType {resp.order_type}"
            )

    def test_buy_produces_limit_with_price(self):
        """v2: geçerli BUY → LIMIT, price=entryRange.max, allow_order=True."""
        engine = RiskEngine(_cfg())
        req = _make_request()
        dec = _make_buy_decision(
            entry_range=EntryRange(min=95.0, max=102.0),
            stop_loss=93.0,
            target_price=116.0,
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.requires_confirmation is False
        assert resp.order_type == OrderType.LIMIT
        assert resp.price == 102.0

    def test_sell_produces_limit_with_price(self):
        """v2: geçerli SELL → LIMIT, price=lastPrice, allow_order=True."""
        engine = RiskEngine(_cfg())
        req = _make_request(
            symbol="THYAO",
            botPositionQty=10,
            totalAccountQty=20,
            lastPrice=100.0,
        )
        dec = RiskDecision(
            action=SignalAction.SELL,
            confidence=85.0,
            reason="Take profit",
            qty=5,
            entry_range=EntryRange(min=97.0, max=102.0),
        )
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.requires_confirmation is False
        assert resp.order_type == OrderType.LIMIT
        assert resp.price == 100.0


class TestEntryRangeParsing:
    """_parse_entry_range handles camelCase + snake_case dicts."""

    def test_camel_case_nested(self):
        from app.services.evaluator import _parse_entry_range

        result = _parse_entry_range(
            {
                "entryRange": {"min": 100, "max": 105},
            }
        )
        assert result is not None
        assert result.min == 100.0
        assert result.max == 105.0

    def test_snake_case_nested(self):
        from app.services.evaluator import _parse_entry_range

        result = _parse_entry_range(
            {
                "entry_range": {"min": 98, "max": 103},
            }
        )
        assert result is not None
        assert result.min == 98.0
        assert result.max == 103.0

    def test_camel_case_flat(self):
        from app.services.evaluator import _parse_entry_range

        result = _parse_entry_range(
            {
                "entryMin": 95,
                "entryMax": 100,
            }
        )
        assert result is not None
        assert result.min == 95.0
        assert result.max == 100.0

    def test_snake_case_flat(self):
        from app.services.evaluator import _parse_entry_range

        result = _parse_entry_range(
            {
                "entry_min": 97,
                "entry_max": 104,
            }
        )
        assert result is not None
        assert result.min == 97.0
        assert result.max == 104.0

    def test_no_entry_range_returns_none(self):
        from app.services.evaluator import _parse_entry_range

        result = _parse_entry_range({"action": "BUY"})
        assert result is None

    def test_garbage_entry_range_returns_none(self):
        """Garbage values in entryRange don't raise."""
        from app.services.evaluator import _parse_entry_range

        assert (
            _parse_entry_range(
                {
                    "entryRange": {"min": "n/a", "max": "???"},
                }
            )
            is None
        )

    def test_entry_range_with_one_missing_returns_none(self):
        """If min present but max missing → None."""
        from app.services.evaluator import _parse_entry_range

        assert (
            _parse_entry_range(
                {
                    "entryRange": {"min": 95},
                }
            )
            is None
        )

    def test_non_numeric_entry_range_returns_none(self):
        """String values that aren't numbers → None."""
        from app.services.evaluator import _parse_entry_range

        assert (
            _parse_entry_range(
                {
                    "entry_range": {"min": "high", "max": "low"},
                }
            )
            is None
        )


class TestDailyTradeCount:
    """Check 4: dailyTradeCount ≥ maxDailyTradeCount → BUY/SELL blocked."""

    def test_buy_blocked_at_limit(self):
        """dailyTradeCount=3, maxDailyTradeCount=3 → BUY blocked."""
        engine = RiskEngine(_cfg(max_daily_trade_count=3))
        req = _make_request(symbol="THYAO", dailyTradeCount=3)
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.action == SignalAction.WAIT
        assert "daily trade count limit reached" in resp.reason.lower()
        assert "3/3" in resp.reason

    def test_buy_blocked_over_limit(self):
        """dailyTradeCount=5, maxDailyTradeCount=3 → BUY blocked."""
        engine = RiskEngine(_cfg(max_daily_trade_count=3))
        req = _make_request(symbol="THYAO", dailyTradeCount=5)
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.action == SignalAction.WAIT
        assert "daily trade count limit reached" in resp.reason.lower()

    def test_sell_blocked_at_limit(self):
        """dailyTradeCount=3, maxDailyTradeCount=3 → SELL blocked."""
        engine = RiskEngine(_cfg(max_daily_trade_count=3))
        req = _make_request(
            symbol="THYAO",
            
            totalAccountQty=20,
            botPositionQty=10,
            dailyTradeCount=3,
        )
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is False
        assert resp.action == SignalAction.WAIT
        assert "daily trade count limit reached" in resp.reason.lower()

    def test_buy_succeeds_below_limit(self):
        """dailyTradeCount=2, maxDailyTradeCount=3 → BUY risk kontrollerine devam eder."""
        engine = RiskEngine(_cfg(max_daily_trade_count=3))
        req = _make_request(symbol="THYAO", dailyTradeCount=2)
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.BUY

    def test_sell_succeeds_below_limit(self):
        """dailyTradeCount=2, maxDailyTradeCount=3 → SELL risk kontrollerine devam eder."""
        engine = RiskEngine(_cfg(max_daily_trade_count=3))
        req = _make_request(
            symbol="THYAO",
            
            totalAccountQty=20,
            botPositionQty=10,
            dailyTradeCount=2,
        )
        dec = RiskDecision(action=SignalAction.SELL, confidence=85.0, qty=5)
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True
        assert resp.action == SignalAction.SELL

    def test_wait_not_blocked_at_limit(self):
        """WAIT kararları dailyTradeCount'tan etkilenmez."""
        engine = RiskEngine(_cfg(max_daily_trade_count=3))
        req = _make_request(symbol="THYAO", dailyTradeCount=5)
        dec = RiskDecision(action=SignalAction.WAIT, confidence=90.0)
        resp = engine.evaluate(req, dec)
        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False  # MANUAL always false
        # Reason should NOT mention daily trade count
        assert "daily trade count" not in resp.reason.lower()

    def test_default_daily_trade_count_zero(self):
        """dailyTradeCount belirtilmezse 0 kabul edilir → limiti aşmaz."""
        engine = RiskEngine(_cfg(max_daily_trade_count=3))
        req = _make_request(
            symbol="THYAO"
        )  # dailyTradeCount defaults to 0
        dec = _make_buy_decision()
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True


class TestCutoffTime:
    """Check 3: cutoff sonrası BUY/SELL engellenir, WAIT etkilenmez."""

    def test_can_trade_now_converts_to_configured_timezone(self):
        """UTC timestamp is evaluated against the configured trading timezone."""
        cfg = _cfg(disable_trading_after="17:30", timezone="Europe/Istanbul")

        before_cutoff_utc = datetime(2026, 7, 7, 14, 29, tzinfo=timezone.utc)
        after_cutoff_utc = datetime(2026, 7, 7, 14, 31, tzinfo=timezone.utc)

        assert cfg.can_trade_now(before_cutoff_utc) is True
        assert cfg.can_trade_now(after_cutoff_utc) is False

    def test_can_trade_now_uses_zoneinfo_when_now_omitted(self, monkeypatch):
        """No explicit now => datetime.now(ZoneInfo(RISK_TIMEZONE))."""
        import app.core.risk_config as risk_config_module

        class FakeDateTime(datetime):
            seen_tz = None

            @classmethod
            def now(cls, tz=None):
                cls.seen_tz = tz
                return datetime(2026, 7, 7, 12, 0, tzinfo=tz)

        monkeypatch.setattr(risk_config_module, "datetime", FakeDateTime)
        cfg = _cfg(disable_trading_after="17:30", timezone="Europe/Istanbul")

        assert cfg.can_trade_now() is True
        assert str(FakeDateTime.seen_tz) == "Europe/Istanbul"

    def test_risk_timezone_env_var(self, monkeypatch):
        """RISK_TIMEZONE overrides the default timezone."""
        monkeypatch.setenv("RISK_TIMEZONE", "UTC")

        cfg = RiskConfig(_env_file="")

        assert cfg.timezone == "UTC"

    def test_buy_blocked_after_cutoff(self, monkeypatch):
        """can_trade_now() returns False → BUY blocked."""
        from app.core.risk_config import RiskConfig

        monkeypatch.setattr(RiskConfig, "can_trade_now", lambda self, now=None: False)
        engine = RiskEngine(_cfg(disable_trading_after="17:30"))
        req = _make_request(symbol="THYAO")

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
            symbol="THYAO",
            
            totalAccountQty=20,
            botPositionQty=10,
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
        req = _make_request(symbol="THYAO")

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
        req = _make_request(symbol="THYAO", lastPrice=100)
        dec = _make_buy_decision(confidence=85.0, qty=5)  # 5*100 = 500
        resp = engine.evaluate(req, dec)
        assert resp.allow_order is True


class TestTechnicalFeatureGuards:
    """Optional Matriks-derived features can veto unsafe directional trades."""

    def test_alpha_trend_sell_blocks_buy(self):
        engine = RiskEngine(_cfg())
        req = _make_request(
            symbol="THYAO",
            
            alphaTrendSignal="SELL",
        )
        resp = engine.evaluate(req, _make_buy_decision())

        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False
        assert "alphaTrendSignal=SELL" in resp.reason

    def test_strong_indicator_sell_consensus_blocks_buy(self):
        engine = RiskEngine(_cfg())
        req = _make_request(
            symbol="THYAO",
            
            indicatorConsensus="SELL",
            indicatorSellCount=4,
        )
        resp = engine.evaluate(req, _make_buy_decision())

        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False
        assert "indicatorConsensus=SELL" in resp.reason

    def test_weak_opposing_consensus_does_not_block_by_itself(self):
        engine = RiskEngine(_cfg())
        req = _make_request(
            symbol="THYAO",
            
            indicatorConsensus="SELL",
            indicatorSellCount=2,
        )
        resp = engine.evaluate(req, _make_buy_decision())

        assert resp.action == SignalAction.BUY
        assert resp.allow_order is True

    def test_high_natr_blocks_new_buy(self):
        engine = RiskEngine(_cfg(max_natr_for_buy=8.0))
        req = _make_request(symbol="THYAO", natr=12.5)
        resp = engine.evaluate(req, _make_buy_decision())

        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False
        assert "nATR" in resp.reason

    def test_depth_queue_drop_blocks_new_buy(self):
        engine = RiskEngine(_cfg(max_depth_queue_drop_pct_for_buy=35.0))
        req = _make_request(
            symbol="THYAO",
            
            depthQueueDropPct=42.0,
        )
        resp = engine.evaluate(req, _make_buy_decision())

        assert resp.action == SignalAction.WAIT
        assert resp.allow_order is False
        assert "bid queue dropped" in resp.reason
