from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import ResearchCandidate, SystemConfig, TradeWatchlistSymbol
from app.models.signal import (
    EntryRange,
    OrderType,
    SignalAction,
    SignalMode,
    SignalRequest,
    SignalResponse,
)
from app.routers.gateway_config import gateway_runtime_config
from app.services.evaluator import (
    EvaluationResult,
    evaluate_symbol,
    with_trade_eligibility,
)
from app.services.research_pipeline import (
    ResearchPolicy,
    apply_research_result,
    list_trade_eligible_symbols,
    maintain_trade_watchlist,
    run_research_cycle,
)
from app.services.matriks_gateway import MatriksGatewayClient
from tests.fake_gateway import FakeGateway


@pytest.fixture(autouse=True)
def _reset_db():
    asyncio.run(drop_all())
    asyncio.run(init_db())
    yield
    asyncio.run(drop_all())
    asyncio.run(init_db())


def _policy(**overrides: Any) -> ResearchPolicy:
    values: dict[str, Any] = {
        "minimum_pass_interval_minutes": 10,
        "consecutive_passes": 2,
    }
    values.update(overrides)
    return ResearchPolicy(**values)


async def _seed_candidate(
    symbol: str = "GARAN", *, summary: dict[str, Any] | None = None
) -> None:
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        session.add(
            ResearchCandidate(
                symbol=symbol,
                status="RESEARCH_PENDING",
                source=["GAINER", "RELATIVE_VOLUME"],
                trend_pre_score=75,
                change_pct_daily=3.2,
                volume_tl=500_000_000,
                relative_volume=2.1,
                technical_summary=summary
                or {
                    "spreadPct": 0.20,
                    "depthReliable": True,
                    "priceAboveEma20": True,
                    "emaTrendAligned": True,
                    "ema20Slope": 0.5,
                    "limitLocked": False,
                },
                expires_at=now + timedelta(hours=24),
            )
        )
        await session.commit()


def _result(
    symbol: str = "GARAN",
    *,
    action: SignalAction = SignalAction.BUY,
    research_score: float = 82,
    confidence: float = 84,
    risk: float = 25,
    target: Decimal = Decimal("112"),
) -> EvaluationResult:
    response = SignalResponse(
        requestId=f"{symbol}-research",
        symbol=symbol,
        action=action,
        qty=0,
        orderType=OrderType.NONE,
        price=None,
        confidenceScore=confidence,
        riskScore=risk,
        allowOrder=False,
        requiresConfirmation=False,
        reason="research result",
        entryRange=EntryRange(min=Decimal("99"), max=Decimal("100")),
        stopLoss=Decimal("95"),
        targetPrice=target,
    )
    return EvaluationResult(
        response=response,
        mode=SignalMode.PAPER,
        evaluation_purpose="RESEARCH_DISCOVERY",
        research_score=research_score,
        raw_action=action,
    )


class TestPromotion:
    async def test_single_pass_does_not_promote(self):
        await _seed_candidate()
        promoted = await apply_research_result("GARAN", _result(), policy=_policy())
        assert promoted is False
        assert await list_trade_eligible_symbols() == []
        async with async_session_factory() as session:
            row = (
                await session.execute(
                    select(ResearchCandidate).where(ResearchCandidate.symbol == "GARAN")
                )
            ).scalar_one()
        assert row.status == "QUALIFIED"
        assert row.consecutive_pass_count == 1

    async def test_two_spaced_passes_promote(self):
        await _seed_candidate()
        # Anchored to "now" (not a fixed date) so the promoted watchlist row's
        # TTL-based expires_at never drifts into the past as real time advances.
        first = datetime.now(UTC) - timedelta(minutes=30)
        await apply_research_result("GARAN", _result(), policy=_policy(), now=first)
        promoted = await apply_research_result(
            "GARAN", _result(), policy=_policy(), now=first + timedelta(minutes=11)
        )
        assert promoted is True
        assert await list_trade_eligible_symbols() == ["GARAN"]
        async with async_session_factory() as session:
            watchlist = (
                await session.execute(
                    select(TradeWatchlistSymbol).where(
                        TradeWatchlistSymbol.symbol == "GARAN"
                    )
                )
            ).scalar_one()
        assert watchlist.is_active is True

    async def test_second_pass_before_minimum_interval_does_not_promote(self):
        await _seed_candidate()
        # Anchored to "now" (not a fixed date) so the promoted watchlist row's
        # TTL-based expires_at never drifts into the past as real time advances.
        first = datetime.now(UTC) - timedelta(minutes=30)
        await apply_research_result("GARAN", _result(), policy=_policy(), now=first)
        assert not await apply_research_result(
            "GARAN", _result(), policy=_policy(), now=first + timedelta(minutes=9)
        )
        assert await list_trade_eligible_symbols() == []
        async with async_session_factory() as session:
            row = (
                await session.execute(
                    select(ResearchCandidate).where(ResearchCandidate.symbol == "GARAN")
                )
            ).scalar_one()
        assert row.consecutive_pass_count == 1
        assert row.rejection_reason == "PROMOTION_PASSES_NOT_TIME_SPACED"

    async def test_low_research_score_stays_researched_and_resets_passes(self):
        await _seed_candidate()
        assert not await apply_research_result(
            "GARAN", _result(research_score=60), policy=_policy()
        )
        async with async_session_factory() as session:
            row = (
                await session.execute(
                    select(ResearchCandidate).where(ResearchCandidate.symbol == "GARAN")
                )
            ).scalar_one()
        assert row.status == "RESEARCHED"
        assert row.consecutive_pass_count == 0
        assert row.rejection_reason == "PROMOTION_RESEARCH_SCORE_BELOW_MINIMUM"

    async def test_high_confidence_high_risk_rejected(self):
        await _seed_candidate()
        assert not await apply_research_result(
            "GARAN", _result(confidence=95, risk=70), policy=_policy()
        )

    async def test_declined_symbol_never_promotes(self):
        await _seed_candidate()
        policy = _policy(declined_symbols=frozenset({"GARAN"}))
        assert not await apply_research_result("GARAN", _result(), policy=policy)

    async def test_reward_risk_below_1_5_rejected(self):
        await _seed_candidate()
        assert not await apply_research_result(
            "GARAN", _result(target=Decimal("106")), policy=_policy()
        )


class TestRemoval:
    async def test_broken_ema_removes_symbol_for_new_buy(self):
        await _seed_candidate(summary={"priceAboveEma20": False, "spreadPct": 0.2})
        now = datetime.now(UTC)
        async with async_session_factory() as session:
            candidate = (
                await session.execute(
                    select(ResearchCandidate).where(ResearchCandidate.symbol == "GARAN")
                )
            ).scalar_one()
            candidate.last_evaluated_at = now
            candidate.ai_research_score = 80
            session.add(
                TradeWatchlistSymbol(
                    symbol="GARAN",
                    is_active=True,
                    expires_at=now + timedelta(hours=24),
                )
            )
            await session.commit()
        assert await maintain_trade_watchlist(set()) == ["GARAN"]
        assert await list_trade_eligible_symbols() == []


class TestResearchBudget:
    async def test_candidate_and_concurrency_limits_are_enforced(self):
        for symbol in ("AAA", "BBB", "CCC"):
            await _seed_candidate(symbol)
        async with async_session_factory() as session:
            session.add_all(
                [
                    SystemConfig(
                        key="maxResearchCandidatesPerCycle",
                        value="2",
                        value_type="int",
                    ),
                    SystemConfig(
                        key="maxConcurrentResearchEvaluations",
                        value="1",
                        value_type="int",
                    ),
                ]
            )
            await session.commit()

        active = 0
        max_active = 0
        calls: list[str] = []

        async def fake_evaluator(symbol: str, **kwargs: Any) -> EvaluationResult:
            nonlocal active, max_active
            assert kwargs["evaluation_purpose"] == "RESEARCH_DISCOVERY"
            assert kwargs["force_paper"] is True
            assert kwargs["mode"] is SignalMode.PAPER
            active += 1
            max_active = max(max_active, active)
            calls.append(symbol)
            await asyncio.sleep(0)
            active -= 1
            return _result(symbol, action=SignalAction.WAIT, research_score=20)

        evaluated = await run_research_cycle(object(), evaluator=fake_evaluator)
        assert len(evaluated) == 2
        assert len(calls) == 2
        assert max_active == 1


class _ResearchProvider:
    async def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        assert payload["schemaVersion"] == "ai-decision-context-v1"
        assert payload["evaluationPurpose"] == "RESEARCH_DISCOVERY"
        assert "allowOrder" not in payload
        return {
            "action": "BUY",
            "confidence": 90,
            "risk_score": 20,
            "research_score": 85,
            "reason": "strong research setup",
            "entry_range": {"min": 70, "max": 71.5},
            "stop_loss": 68,
            "target_price": 77,
        }


class TestResearchOrderIsolation:
    async def test_candidate_is_subscribed_but_not_gateway_buy_eligible(self):
        await _seed_candidate("THYAO")

        config = await gateway_runtime_config()

        assert "THYAO" in config["subscriptionSymbols"]
        assert "THYAO" not in config["tradeEligibleSymbols"]
        assert "THYAO" not in config["buyAllowedSymbols"]

    async def test_client_cannot_spoof_trade_eligibility(self):
        await _seed_candidate("THYAO")
        request = SignalRequest(
            requestId="spoofed-eligibility",
            symbol="THYAO",
            timeframe="Min5",
            lastPrice=Decimal("71.5"),
            open=Decimal("71"),
            high=Decimal("72"),
            low=Decimal("70"),
            volume=Decimal("1000"),
            tradeEligible=True,
            mode=SignalMode.DEMO_LIVE,
        )

        resolved = await with_trade_eligibility(request)

        assert resolved.trade_eligible is False

    async def test_research_evaluation_can_never_authorize_order(self):
        fake = FakeGateway()
        gateway = MatriksGatewayClient(
            base_url="http://fake", token=fake.token, transport=fake.transport
        )
        result = await evaluate_symbol(
            "THYAO",
            gateway=gateway,
            provider=_ResearchProvider(),
            mode=SignalMode.DEMO_LIVE,
            evaluation_purpose="RESEARCH_DISCOVERY",
        )
        assert result is not None
        assert result.mode == SignalMode.PAPER
        assert result.response.action == SignalAction.BUY
        assert result.response.allow_order is False
        assert result.response.qty == 0
