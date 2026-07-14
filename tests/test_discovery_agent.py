"""Tests for the movers-based discovery agent and its watchlist persistence."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import ResearchCandidate, WatchlistQualityScore, WatchlistSymbol
from app.services.discovery_agent import (
    _ask_bid_ratio,
    list_active_watchlist_symbols,
    run_discovery_scan,
)
from app.services.matriks_gateway import GatewayUnavailable


@pytest.fixture(autouse=True)
def _reset_db():
    asyncio.run(drop_all())
    asyncio.run(init_db())
    yield
    asyncio.run(drop_all())
    asyncio.run(init_db())


class FakeGateway:
    """get_movers + get_depth sunan sahte gateway."""

    def __init__(self, movers=None, depth_by_symbol=None, raise_exc=None):
        self._movers = movers
        self._depth = depth_by_symbol or {}
        self._raise = raise_exc

    async def get_movers(self, limit: int = 20):
        if self._raise is not None:
            raise self._raise
        return self._movers

    async def get_depth(self, symbol: str, levels: int = 25):
        depth = self._depth.get(symbol.upper())
        if depth is None:
            raise GatewayUnavailable("no depth")
        return depth


def _movers(items, *, gainers=(), losers=(), volume_leaders=()):
    return {
        "ok": True,
        "available": True,
        "items": items,
        "gainers": list(gainers),
        "losers": list(losers),
        "volumeLeaders": list(volume_leaders),
    }


def _item(symbol, change_pct, volume):
    return {
        "symbol": symbol,
        "lastPrice": 100.0,
        "changePct": change_pct,
        "volume": volume,
    }


def _depth(bid_total, ask_total):
    return {
        "ok": True,
        "bids": [{"level": 1, "price": 99.0, "size": bid_total}],
        "asks": [{"level": 1, "price": 100.0, "size": ask_total}],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Eleme kuralları
# ═══════════════════════════════════════════════════════════════════════════════


class TestScreening:
    async def test_healthy_candidate_is_accepted(self):
        gw = FakeGateway(
            _movers(
                [_item("GARAN", 3.5, 500_000_000)],
                gainers=["GARAN"],
            ),
            depth_by_symbol={"GARAN": _depth(bid_total=10_000, ask_total=12_000)},
        )

        added = await run_discovery_scan(gw)

        assert added == ["GARAN"]
        assert await list_active_watchlist_symbols() == ["GARAN"]
        async with async_session_factory() as session:
            score = (
                await session.execute(
                    select(WatchlistQualityScore).where(
                        WatchlistQualityScore.symbol == "GARAN"
                    )
                )
            ).scalar_one()
        assert score.depth_score > 50
        assert score.reason_json["askBidRatio"] == 1.2
        async with async_session_factory() as session:
            candidate = (
                await session.execute(
                    select(ResearchCandidate).where(
                        ResearchCandidate.symbol == "GARAN"
                    )
                )
            ).scalar_one()
        assert candidate.status == "RESEARCH_PENDING"
        assert candidate.trend_pre_score >= 60

    async def test_limit_locked_gainer_is_rejected(self):
        """Tavan kitlemiş (+%9.8) aday elenir."""
        gw = FakeGateway(
            _movers([_item("SOKE", 9.8, 900_000_000)], gainers=["SOKE"]),
            depth_by_symbol={"SOKE": _depth(10_000, 10_000)},
        )

        assert await run_discovery_scan(gw) == []

    async def test_floor_locked_loser_is_rejected(self):
        gw = FakeGateway(
            _movers([_item("XYZ", -9.9, 900_000_000)], losers=["XYZ"]),
        )

        assert await run_discovery_scan(gw) == []

    async def test_thin_volume_is_rejected(self):
        gw = FakeGateway(
            _movers([_item("TINY", 4.0, 5_000_000)], gainers=["TINY"]),
        )

        assert await run_discovery_scan(gw) == []

    async def test_sell_wall_is_rejected(self):
        """Ask/bid oranı 3'ü aşan (satış duvarı) aday elenir."""
        gw = FakeGateway(
            _movers([_item("WALL", 4.0, 500_000_000)], gainers=["WALL"]),
            depth_by_symbol={"WALL": _depth(bid_total=1_000, ask_total=5_000)},
        )

        assert await run_discovery_scan(gw) == []

    async def test_missing_depth_does_not_block_acceptance(self):
        """Derinlik alınamıyorsa duvar filtresi atlanır (fail-open)."""
        gw = FakeGateway(
            _movers([_item("NODEPTH", 4.0, 500_000_000)], gainers=["NODEPTH"]),
        )

        assert await run_discovery_scan(gw) == ["NODEPTH"]

    async def test_symbol_in_multiple_lists_processed_once(self):
        gw = FakeGateway(
            _movers(
                [_item("BIG", 4.0, 900_000_000)],
                gainers=["BIG"],
                volume_leaders=["BIG"],
            ),
        )

        added = await run_discovery_scan(gw)

        assert added == ["BIG"]
        async with async_session_factory() as session:
            rows = (await session.execute(select(WatchlistSymbol))).scalars().all()
        assert len(rows) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Fail-open + upsert davranışı
# ═══════════════════════════════════════════════════════════════════════════════


class TestFailOpenAndUpsert:
    async def test_gateway_down_returns_empty(self):
        gw = FakeGateway(raise_exc=GatewayUnavailable("down"))
        assert await run_discovery_scan(gw) == []

    async def test_unavailable_movers_returns_empty(self):
        gw = FakeGateway({"ok": True, "available": False})
        assert await run_discovery_scan(gw) == []

    async def test_rescan_updates_existing_row_not_duplicate(self):
        gw = FakeGateway(
            _movers([_item("GARAN", 3.5, 500_000_000)], gainers=["GARAN"]),
        )
        await run_discovery_scan(gw)

        gw2 = FakeGateway(
            _movers([_item("GARAN", 5.1, 600_000_000)], gainers=["GARAN"]),
        )
        await run_discovery_scan(gw2)

        async with async_session_factory() as session:
            rows = (await session.execute(select(WatchlistSymbol))).scalars().all()
        assert len(rows) == 1
        assert rows[0].change_pct == 5.1


# ═══════════════════════════════════════════════════════════════════════════════
# _ask_bid_ratio
# ═══════════════════════════════════════════════════════════════════════════════


class TestAskBidRatio:
    def test_ratio_computed_from_levels(self):
        assert _ask_bid_ratio(_depth(bid_total=1_000, ask_total=3_000)) == 3.0

    def test_missing_side_returns_none(self):
        assert _ask_bid_ratio({"bids": [], "asks": [{"size": 100}]}) is None
