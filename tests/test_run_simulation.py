from __future__ import annotations

import asyncio

from app.db.init_db import drop_all, init_db
from run_simulation import run_simulation


def test_offline_simulation_seeds_and_replays_without_gateway():
    async def run():
        await drop_all()
        await init_db()
        result = await run_simulation("THYAO", None, "PAPER")
        assert result["gatewayCalled"] is False
        assert result["recordedAiDecision"]["action"] == "BUY"
        assert result["riskReplay"]["requestId"] == result["requestId"]
        assert result["riskReplay"]["mode"] == "PAPER"

    asyncio.run(run())
