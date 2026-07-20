"""Tests for the movers-based discovery agent and its watchlist persistence."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import (
    ResearchCandidate,
    SystemConfig,
    WatchlistQualityScore,
)
from app.routers.gateway_config import gateway_runtime_config
from app.services.discovery_agent import (
    _HISTORICAL_BARS_CACHE,
    _ask_bid_ratio,
    _historical_bar_metrics,
    DiscoveryScanResult,
    list_active_watchlist_symbols,
    run_discovery_scan,
)
from app.services.matriks_gateway import GatewayUnavailable


@pytest.fixture(autouse=True)
def _reset_db():
    asyncio.run(drop_all())
    asyncio.run(init_db())
    asyncio.run(_set_scan_universe())
    _HISTORICAL_BARS_CACHE.clear()
    yield
    asyncio.run(drop_all())
    asyncio.run(init_db())


async def _set_scan_universe(symbols: str | None = None) -> None:
    symbols = symbols or "GARAN,SOKE,XYZ,TINY,WALL,NODEPTH,BIG"
    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(SystemConfig).where(SystemConfig.key == "scanUniverseSymbols")
            )
        ).scalar_one_or_none()
        if row is None:
            # SystemConfig rows are created lazily (see admin_config.get_admin_config_value's
            # fail-open default); a fresh init_db() does not pre-seed them.
            session.add(
                SystemConfig(
                    key="scanUniverseSymbols",
                    value=symbols,
                    value_type="string",
                )
            )
        else:
            row.value = symbols
        await session.commit()


class FakeGateway:
    """get_movers + get_depth sunan sahte gateway."""

    def __init__(
        self, movers=None, depth_by_symbol=None, bars_by_symbol=None, raise_exc=None
    ):
        self._movers = movers
        self._depth = depth_by_symbol or {}
        self._bars = bars_by_symbol or {}
        self._raise = raise_exc
        self.bar_calls: list[str] = []

    async def get_movers(self, limit: int = 20):
        if self._raise is not None:
            raise self._raise
        return self._movers

    async def get_market_ranking_capabilities(self):
        return (self._movers or {}).get("rankingCapabilities", {})

    async def get_bars(self, symbol: str, count: int = 50):
        self.bar_calls.append(symbol.upper())
        return self._bars.get(symbol.upper(), {"available": False})

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
    # Trend-pre-score is fail-closed for missing technical fields (no partial
    # credit), so a "healthy candidate" fixture must supply EMA/RSI/quote-age
    # data to clear DiscoveryPolicy.minimum_trend_score (60) — matching what a
    # real Matriks snapshot would include.
    return {
        "symbol": symbol,
        "lastPrice": 100.0,
        "changePct": change_pct,
        "volume": volume,
        "sessionTurnoverTl": volume,
        "volumeSemantic": "CUMULATIVE_SESSION_TURNOVER_TL",
        "ema20": 98.0,
        "ema50": 95.0,
        "rsi": 60.0,
        "quoteAgeSeconds": 5,
    }


def _daily_bars(*, latest_volume: float = 200.0, count: int = 21):
    bars = [
        {
            "open": 90 + index,
            "high": 91 + index,
            "low": 89 + index,
            "close": 90 + index,
            "volume": 100.0,
            "reliable": True,
            "closed": True,
        }
        for index in range(count)
    ]
    if bars:
        bars[-1]["volume"] = latest_volume
    return {
        "available": True,
        "period": "Day",
        "actualBarPeriod": "Day",
        "bars": bars,
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
                    select(ResearchCandidate).where(ResearchCandidate.symbol == "GARAN")
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
            rows = (await session.execute(select(ResearchCandidate))).scalars().all()
        assert len(rows) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Fail-open + upsert davranışı
# ═══════════════════════════════════════════════════════════════════════════════


class TestFailOpenAndUpsert:
    async def test_gateway_down_returns_empty(self):
        gw = FakeGateway(raise_exc=GatewayUnavailable("down"))
        result = await run_discovery_scan(gw)

        assert result == []
        assert isinstance(result, DiscoveryScanResult)
        assert result.status == "GATEWAY_UNAVAILABLE"

    async def test_unavailable_movers_returns_empty(self):
        gw = FakeGateway({"ok": True, "available": False})
        result = await run_discovery_scan(gw)

        assert result == []
        assert result.status == "MARKET_DATA_UNAVAILABLE"

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
            rows = (await session.execute(select(ResearchCandidate))).scalars().all()
        assert len(rows) == 1
        assert rows[0].change_pct_daily == 5.1

    async def test_missing_candidate_ttl_is_not_active_and_expires(self):
        async with async_session_factory() as session:
            session.add(
                ResearchCandidate(
                    symbol="LEGACY",
                    status="RESEARCH_PENDING",
                    source=["LEGACY"],
                    trend_pre_score=0,
                    expires_at=None,
                )
            )
            await session.commit()

        config = await gateway_runtime_config()
        assert "LEGACY" not in config["subscriptionSymbols"]
        assert await list_active_watchlist_symbols() == []

        await run_discovery_scan(FakeGateway(_movers([])))

        async with async_session_factory() as session:
            candidate = (
                await session.execute(
                    select(ResearchCandidate).where(
                        ResearchCandidate.symbol == "LEGACY"
                    )
                )
            ).scalar_one()
        assert candidate.status == "EXPIRED"
        assert candidate.expires_at is None


class TestHistoricalBarsFallback:
    async def test_only_configured_universe_is_ranked(self):
        await _set_scan_universe("GARAN")
        gw = FakeGateway(
            _movers(
                [_item("GARAN", 3.0, 500_000_000), _item("SOKE", 4.0, 600_000_000)],
                gainers=["SOKE", "GARAN"],
            ),
            bars_by_symbol={"GARAN": _daily_bars()},
        )

        result = await run_discovery_scan(gw)

        assert result.universe_count == 1
        assert gw.bar_calls == ["GARAN"]

    async def test_daily_bars_supply_weekly_and_relative_volume_rankings(self):
        movers = _movers([_item("GARAN", 3.0, 500_000_000)], gainers=["GARAN"])
        movers["rankingCapabilities"] = {
            "nativeMarketWide": False,
            "weeklyGainers": {"available": False},
            "turnoverLeaders": {"available": False},
            "relativeVolumeLeaders": {"available": False},
        }
        gw = FakeGateway(movers, bars_by_symbol={"GARAN": _daily_bars()})

        result = await run_discovery_scan(gw)

        assert result.ranking_scope == "HISTORICAL_BARS_FALLBACK"
        assert result.historical_bar_requested_count == 1
        assert result.historical_bar_success_count == 1
        assert result.weekly_gainer_count == 1
        assert result.relative_volume_leader_count == 1
        assert result.unavailable_signals == {}

    async def test_insufficient_volume_baseline_never_invents_relative_volume(self):
        weekly, relative_volume, reasons = _historical_bar_metrics(_daily_bars(count=6))

        assert weekly is not None
        assert relative_volume is None
        assert reasons["RELATIVE_VOLUME"] == (
            "HISTORICAL_RELATIVE_VOLUME_BASELINE_INSUFFICIENT"
        )

    async def test_historical_bars_are_reused_within_ttl(self):
        movers = _movers([_item("GARAN", 3.0, 500_000_000)], gainers=["GARAN"])
        gw = FakeGateway(movers, bars_by_symbol={"GARAN": _daily_bars()})

        await run_discovery_scan(gw)
        await run_discovery_scan(gw)

        assert gw.bar_calls == ["GARAN"]


# ═══════════════════════════════════════════════════════════════════════════════
# _ask_bid_ratio
# ═══════════════════════════════════════════════════════════════════════════════


class TestAskBidRatio:
    def test_ratio_computed_from_levels(self):
        assert _ask_bid_ratio(_depth(bid_total=1_000, ask_total=3_000)) == 3.0

    def test_missing_side_returns_none(self):
        assert _ask_bid_ratio({"bids": [], "asks": [{"size": 100}]}) is None
