"""Tests for the weekly self-reflection review agent (Task 7)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import AiDecision, AiLessonLearned, OrderLog, RiskDecision
from app.services.review_agent import (
    build_review_payload,
    find_closed_trades,
    previous_week_bounds,
    run_weekly_review,
)

TZ = ZoneInfo("Europe/Istanbul")


@pytest.fixture(autouse=True)
def _reset_db():
    asyncio.run(drop_all())
    asyncio.run(init_db())
    yield
    asyncio.run(drop_all())
    asyncio.run(init_db())


class FakeChatProvider:
    def __init__(self, response_text: str = ""):
        self.response_text = response_text
        self.calls: list[tuple[str, str]] = []

    async def decide(self, payload, *, request_id=None):  # pragma: no cover — unused here
        raise NotImplementedError

    async def chat(
        self, system_prompt: str, user_content: str, *, max_tokens: int = 800
    ) -> str:
        self.calls.append((system_prompt, user_content))
        return self.response_text


async def _seed_round_trip(
    symbol: str,
    *,
    buy_price: float,
    sell_price: float,
    qty: float = 10.0,
    stop_loss: float | None = None,
    buy_at: datetime,
    sell_at: datetime,
    request_prefix: str,
    entry_reason: str = "RSI oversold bounce",
    entry_confidence: float = 80.0,
    raw_request: dict | None = None,
):
    buy_request_id = f"{request_prefix}-buy"
    sell_request_id = f"{request_prefix}-sell"
    async with async_session_factory() as session:
        session.add(
            OrderLog(
                request_id=buy_request_id,
                symbol=symbol,
                action="BUY",
                qty=qty,
                price=buy_price,
                status="FILLED",
                created_at=buy_at,
            )
        )
        session.add(
            OrderLog(
                request_id=sell_request_id,
                symbol=symbol,
                action="SELL",
                qty=qty,
                price=sell_price,
                status="FILLED",
                created_at=sell_at,
            )
        )
        session.add(
            RiskDecision(
                request_id=buy_request_id,
                symbol=symbol,
                action="BUY",
                confidence=entry_confidence,
                stop_loss=stop_loss,
                target_price=(buy_price * 1.1) if stop_loss else None,
                created_at=buy_at,
            )
        )
        session.add(
            AiDecision(
                request_id=buy_request_id,
                symbol=symbol,
                reason=entry_reason,
                confidence=entry_confidence,
                action="BUY",
                raw_request=raw_request or {"symbol": symbol},
                created_at=buy_at,
            )
        )
        await session.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# previous_week_bounds
# ═══════════════════════════════════════════════════════════════════════════════


class TestPreviousWeekBounds:
    def test_bounds_are_monday_to_monday(self):
        # 2026-07-15 bir Çarşamba — önceki hafta 06-07 (Pzt) .. 13-07 (Pzt)
        start, end = previous_week_bounds(date(2026, 7, 15))

        assert start == datetime(2026, 7, 6, tzinfo=TZ)
        assert end == datetime(2026, 7, 13, tzinfo=TZ)

    def test_reference_on_monday_still_returns_prior_full_week(self):
        start, end = previous_week_bounds(date(2026, 7, 13))  # bu da Pazartesi

        assert start == datetime(2026, 7, 6, tzinfo=TZ)
        assert end == datetime(2026, 7, 13, tzinfo=TZ)


# ═══════════════════════════════════════════════════════════════════════════════
# find_closed_trades + is_stop_loss_hit
# ═══════════════════════════════════════════════════════════════════════════════


class TestFindClosedTrades:
    async def test_matches_buy_and_sell_computes_pnl(self):
        buy_at = datetime(2026, 7, 7, 10, 0, tzinfo=TZ)
        sell_at = datetime(2026, 7, 8, 11, 0, tzinfo=TZ)
        await _seed_round_trip(
            "THYAO",
            buy_price=100.0,
            sell_price=95.0,
            stop_loss=95.5,
            buy_at=buy_at,
            sell_at=sell_at,
            request_prefix="thyao-1",
        )

        async with async_session_factory() as session:
            trades = await find_closed_trades(
                session,
                datetime(2026, 7, 6, tzinfo=TZ),
                datetime(2026, 7, 13, tzinfo=TZ),
            )

        assert len(trades) == 1
        t = trades[0]
        assert t.symbol == "THYAO"
        assert t.realized_pnl == -50.0  # (95-100)*10
        assert round(t.realized_pnl_pct, 2) == -5.0
        assert t.is_stop_loss_hit is True

    async def test_sell_outside_period_excluded(self):
        await _seed_round_trip(
            "AKBNK",
            buy_price=70.0,
            sell_price=68.0,
            stop_loss=68.5,
            buy_at=datetime(2026, 6, 20, tzinfo=TZ),
            sell_at=datetime(2026, 6, 21, tzinfo=TZ),  # önceki hafta değil
            request_prefix="akbnk-1",
        )

        async with async_session_factory() as session:
            trades = await find_closed_trades(
                session,
                datetime(2026, 7, 6, tzinfo=TZ),
                datetime(2026, 7, 13, tzinfo=TZ),
            )

        assert trades == []

    async def test_no_matching_buy_is_skipped(self):
        async with async_session_factory() as session:
            session.add(
                OrderLog(
                    request_id="orphan-sell",
                    symbol="SISE",
                    action="SELL",
                    qty=5.0,
                    price=40.0,
                    status="FILLED",
                    created_at=datetime(2026, 7, 8, tzinfo=TZ),
                )
            )
            await session.commit()

        async with async_session_factory() as session:
            trades = await find_closed_trades(
                session,
                datetime(2026, 7, 6, tzinfo=TZ),
                datetime(2026, 7, 13, tzinfo=TZ),
            )

        assert trades == []

    async def test_profitable_exit_is_not_stop_loss_hit(self):
        await _seed_round_trip(
            "GARAN",
            buy_price=50.0,
            sell_price=55.0,  # kâr
            stop_loss=48.0,
            buy_at=datetime(2026, 7, 7, tzinfo=TZ),
            sell_at=datetime(2026, 7, 8, tzinfo=TZ),
            request_prefix="garan-1",
        )

        async with async_session_factory() as session:
            trades = await find_closed_trades(
                session,
                datetime(2026, 7, 6, tzinfo=TZ),
                datetime(2026, 7, 13, tzinfo=TZ),
            )

        assert trades[0].is_stop_loss_hit is False

    async def test_loss_not_near_stop_is_not_flagged(self):
        """Zararla kapandı ama exit fiyatı stop'un çok üzerinde — stop'a
        isabet değil (ör. manuel/force-sell)."""
        await _seed_round_trip(
            "KCHOL",
            buy_price=100.0,
            sell_price=97.0,
            stop_loss=80.0,  # exit, stop'tan çok uzak
            buy_at=datetime(2026, 7, 7, tzinfo=TZ),
            sell_at=datetime(2026, 7, 8, tzinfo=TZ),
            request_prefix="kchol-1",
        )

        async with async_session_factory() as session:
            trades = await find_closed_trades(
                session,
                datetime(2026, 7, 6, tzinfo=TZ),
                datetime(2026, 7, 13, tzinfo=TZ),
            )

        assert trades[0].is_stop_loss_hit is False


# ═══════════════════════════════════════════════════════════════════════════════
# build_review_payload
# ═══════════════════════════════════════════════════════════════════════════════


class TestBuildReviewPayload:
    async def test_payload_includes_news_and_broker_flow_when_present(self):
        await _seed_round_trip(
            "THYAO",
            buy_price=100.0,
            sell_price=95.0,
            stop_loss=95.5,
            buy_at=datetime(2026, 7, 7, tzinfo=TZ),
            sell_at=datetime(2026, 7, 8, tzinfo=TZ),
            request_prefix="thyao-ctx",
            raw_request={
                "symbol": "THYAO",
                "newsContext": {"THYAO": {"latestNews": [{"title": "haber"}]}},
                "brokerFlowContext": {"THYAO": {"smartMoneyFlow": "STRONG_BUY"}},
            },
        )

        async with async_session_factory() as session:
            trades = await find_closed_trades(
                session,
                datetime(2026, 7, 6, tzinfo=TZ),
                datetime(2026, 7, 13, tzinfo=TZ),
            )
        payload = build_review_payload(trades)

        assert (
            payload[0]["newsContextAtEntry"]["THYAO"]["latestNews"][0]["title"]
            == "haber"
        )
        assert (
            payload[0]["brokerFlowContextAtEntry"]["THYAO"]["smartMoneyFlow"]
            == "STRONG_BUY"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# run_weekly_review — uçtan uca
# ═══════════════════════════════════════════════════════════════════════════════


class TestRunWeeklyReview:
    async def test_no_stop_loss_trades_returns_empty_no_llm_call(self):
        provider = FakeChatProvider()

        result = await run_weekly_review(date(2026, 7, 15), provider=provider)

        assert result == []
        assert provider.calls == []  # token harcanmadı

    async def test_flagged_trades_persist_lesson(self):
        await _seed_round_trip(
            "THYAO",
            buy_price=100.0,
            sell_price=94.0,
            stop_loss=95.0,
            buy_at=datetime(2026, 7, 7, tzinfo=TZ),
            sell_at=datetime(2026, 7, 8, tzinfo=TZ),
            request_prefix="thyao-r",
        )
        provider = FakeChatProvider(
            response_text='{"lessons": [{"rootCause": "STOP_TOO_TIGHT", '
            '"lesson": "Stop was inside normal volatility.", '
            '"proposedRule": "Widen stops when nATR > 5.", '
            '"affectedSymbols": ["THYAO"]}]}'
        )

        result = await run_weekly_review(date(2026, 7, 15), provider=provider)

        assert len(result) == 1
        row = result[0]
        assert row.root_cause == "STOP_TOO_TIGHT"
        assert row.symbols_involved == "THYAO"
        assert row.trades_reviewed_count == 1
        assert row.status == "PENDING_REVIEW"
        assert row.proposed_rule == "Widen stops when nATR > 5."
        assert len(provider.calls) == 1

        async with async_session_factory() as session:
            from sqlalchemy import select

            rows = (await session.execute(select(AiLessonLearned))).scalars().all()
        assert len(rows) == 1

    async def test_unparseable_llm_response_persists_placeholder(self):
        await _seed_round_trip(
            "AKBNK",
            buy_price=70.0,
            sell_price=66.0,
            stop_loss=67.0,
            buy_at=datetime(2026, 7, 7, tzinfo=TZ),
            sell_at=datetime(2026, 7, 8, tzinfo=TZ),
            request_prefix="akbnk-r",
        )
        provider = FakeChatProvider(response_text="not json at all")

        result = await run_weekly_review(date(2026, 7, 15), provider=provider)

        assert len(result) == 1
        assert result[0].root_cause == "OTHER"
        assert "could not be parsed" in result[0].lesson.lower()

    async def test_invalid_root_cause_falls_back_to_other(self):
        await _seed_round_trip(
            "SISE",
            buy_price=40.0,
            sell_price=37.0,
            stop_loss=38.0,
            buy_at=datetime(2026, 7, 7, tzinfo=TZ),
            sell_at=datetime(2026, 7, 8, tzinfo=TZ),
            request_prefix="sise-r",
        )
        provider = FakeChatProvider(
            response_text='{"lessons": [{"rootCause": "MADE_UP_CAUSE", '
            '"lesson": "test", "affectedSymbols": ["SISE"]}]}'
        )

        result = await run_weekly_review(date(2026, 7, 15), provider=provider)

        assert result[0].root_cause == "OTHER"

    async def test_multiple_lessons_from_one_response_all_persisted(self):
        await _seed_round_trip(
            "THYAO",
            buy_price=100.0,
            sell_price=94.0,
            stop_loss=95.0,
            buy_at=datetime(2026, 7, 7, tzinfo=TZ),
            sell_at=datetime(2026, 7, 8, tzinfo=TZ),
            request_prefix="thyao-m",
        )
        await _seed_round_trip(
            "AKBNK",
            buy_price=70.0,
            sell_price=66.0,
            stop_loss=67.0,
            buy_at=datetime(2026, 7, 9, tzinfo=TZ),
            sell_at=datetime(2026, 7, 10, tzinfo=TZ),
            request_prefix="akbnk-m",
        )
        provider = FakeChatProvider(
            response_text='{"lessons": ['
            '{"rootCause": "STOP_TOO_TIGHT", "lesson": "a", "affectedSymbols": ["THYAO"]},'
            '{"rootCause": "NEWS_MISREAD", "lesson": "b", "affectedSymbols": ["AKBNK"]}'
            "]}"
        )

        result = await run_weekly_review(date(2026, 7, 15), provider=provider)

        assert len(result) == 2
        causes = {r.root_cause for r in result}
        assert causes == {"STOP_TOO_TIGHT", "NEWS_MISREAD"}

    async def test_default_provider_used_when_none_given(self, monkeypatch):
        """provider=None -> get_default_provider() çağrılır (mock AI_PROVIDER=mock
        döngüsünde chat() '' döner, placeholder lesson persist edilir)."""
        await _seed_round_trip(
            "THYAO",
            buy_price=100.0,
            sell_price=94.0,
            stop_loss=95.0,
            buy_at=datetime(2026, 7, 7, tzinfo=TZ),
            sell_at=datetime(2026, 7, 8, tzinfo=TZ),
            request_prefix="thyao-def",
        )

        result = await run_weekly_review(date(2026, 7, 15))

        assert len(result) == 1
        assert result[0].root_cause == "OTHER"
