"""Bounded background collector that fills the gaps between scanner and
stop-guard snapshots (Fix 3).

The scanner only snapshots trade-eligible symbols on its scan interval and
the stop-guard only snapshots symbols with an open lifecycle - so a symbol
that was evaluated once (producing a DecisionOutcome) but is neither
trade-eligible nor held would otherwise get no further observations, and its
forward-return horizons could never be measured. This collector periodically
snapshots exactly those "still needs measurement" symbols and records a
MarketObservation for each.

Strictly rate-limited and non-blocking: at most one observation per symbol
per minute, a hard per-run symbol cap, bounded gateway concurrency, and it
is invoked from the scanner tick *before* the trading/cutoff gates so it can
never sit on or delay the order path. A symbol drops out of collection
automatically once its outcomes no longer need any horizon (the query stops
returning it).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.db import DecisionOutcome
from app.services.market_observation import record_market_observation_standalone
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    MatriksGatewayClient,
)

logger = logging.getLogger(__name__)

# At most one observation per symbol per minute (Fix 3).
_PER_SYMBOL_COOLDOWN = timedelta(seconds=60)
# Hard cap on symbols snapshotted in a single run, so a large backlog can
# never turn one tick into a burst of gateway calls.
_MAX_SYMBOLS_PER_RUN = 20
# Bounded concurrency for the gateway snapshot calls.
_CONCURRENCY = 4
# Outcomes older than this are assumed permanently unmeasurable and are not
# kept alive by the collector (the labeler will mark them DATA_GAP).
_MAX_OUTCOME_AGE = timedelta(hours=12)


class MarketObservationCollector:
    """Owns the per-symbol cooldown state; a process-lifetime singleton,
    mirroring StopLossGuard's in-memory state model."""

    def __init__(self) -> None:
        self._last_collected: dict[str, datetime] = {}

    async def _due_symbols(self) -> list[str]:
        """Symbols with an outcome still awaiting measurement (PENDING /
        PARTIAL / DATA_GAP) and recent enough to be worth observing."""
        cutoff = datetime.now(timezone.utc) - _MAX_OUTCOME_AGE
        async with async_session_factory() as session:
            rows = (
                await session.execute(
                    select(DecisionOutcome.symbol)
                    .where(
                        DecisionOutcome.outcome_status.in_(
                            ("PENDING", "PARTIAL", "DATA_GAP")
                        ),
                        DecisionOutcome.decision_at >= cutoff,
                    )
                    .distinct()
                )
            ).all()
        return [row[0].strip().upper() for row in rows]

    def _filter_on_cooldown(self, symbols: list[str], *, now: datetime) -> list[str]:
        due: list[str] = []
        for symbol in symbols:
            last = self._last_collected.get(symbol)
            if last is not None and (now - last) < _PER_SYMBOL_COOLDOWN:
                continue
            due.append(symbol)
            if len(due) >= _MAX_SYMBOLS_PER_RUN:
                break
        return due

    async def _collect_one(
        self, gateway: MatriksGatewayClient, symbol: str, semaphore: asyncio.Semaphore
    ) -> None:
        async with semaphore:
            try:
                snapshot = await gateway.get_snapshot(symbol)
            except (GatewayUnavailable, GatewayError):
                return
            except Exception:
                logger.exception("OBSERVATION_COLLECTOR_SNAPSHOT_FAILED symbol=%s", symbol)
                return
            payload = snapshot.get("payload") or {}
            await record_market_observation_standalone(symbol, payload)
            self._last_collected[symbol] = datetime.now(timezone.utc)

    async def run(self, gateway: MatriksGatewayClient) -> int:
        """Collect one observation for each due symbol; returns how many were
        collected. Never raises - a measurement side-channel must not break
        the scanner tick that calls it."""
        try:
            now = datetime.now(timezone.utc)
            candidates = await self._due_symbols()
            symbols = self._filter_on_cooldown(candidates, now=now)
            if not symbols:
                return 0
            # Drop cooldown entries for symbols that are no longer due, so the
            # dict cannot grow without bound across a long-running process.
            due_set = set(candidates)
            self._last_collected = {
                s: t for s, t in self._last_collected.items() if s in due_set
            }
            semaphore = asyncio.Semaphore(_CONCURRENCY)
            await asyncio.gather(
                *(self._collect_one(gateway, symbol, semaphore) for symbol in symbols)
            )
            logger.info("OBSERVATION_COLLECTOR_RUN collectedSymbols=%s", len(symbols))
            return len(symbols)
        except Exception:
            logger.exception("OBSERVATION_COLLECTOR_RUN_FAILED")
            return 0


market_observation_collector = MarketObservationCollector()
