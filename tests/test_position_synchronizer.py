"""Tests for the independent, order-free position refresh loop."""

from __future__ import annotations

import asyncio

import pytest

from app.services.position_sync import PositionSynchronizer


class NoOrderGateway:
    """A gateway sentinel: the synchronizer must never call order methods."""

    async def send_order(self, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("Position synchronizer must never send orders")


@pytest.mark.asyncio
async def test_sync_once_updates_status_without_order_path():
    gateway = NoOrderGateway()
    calls: list[object] = []

    async def refresh(received_gateway: object) -> int:
        calls.append(received_gateway)
        return 3

    synchronizer = PositionSynchronizer(
        gateway=gateway, interval_seconds=60, sync_func=refresh
    )

    assert await synchronizer.sync_once() == 3
    status = synchronizer.get_status()
    assert calls == [gateway]
    assert status["lastSyncedCount"] == 3
    assert status["lastAttemptAt"] is not None
    assert status["lastCompletedAt"] is not None
    assert status["lastError"] is None


@pytest.mark.asyncio
async def test_background_loop_runs_without_scanner_or_trading_state():
    completed = asyncio.Event()
    calls = 0

    async def refresh(_gateway: object) -> int:
        nonlocal calls
        calls += 1
        completed.set()
        return 0

    synchronizer = PositionSynchronizer(
        gateway=NoOrderGateway(), interval_seconds=60, sync_func=refresh
    )
    synchronizer.start()
    await asyncio.wait_for(completed.wait(), timeout=1)
    await synchronizer.stop()

    assert calls == 1
    assert synchronizer.running is False
