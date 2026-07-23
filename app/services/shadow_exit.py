"""Shadow exit politikası değerlendirmesi (Plan Faz 3.2).

Alternatif exit politikaları AYNI gerçek DEMO girişleri üzerinde, gerçek
piyasa gözlemleri (``market_observations``) yeniden oynatılarak
değerlendirilir. Böylece hiçbir yeni işlem yapmadan "şu politika bu gerçek
girişlerde nasıl çıkardı" sorusu yanıtlanır; dondurulmuş baseline politika da
aday olarak dahil edilip net beklenti karşılaştırılabilir (plan bölüm 10/11).

Çıkış kararı üretim monitörüyle bire bir aynı ``evaluate_exit`` fonksiyonunu
kullanır, dolayısıyla shadow ile canlı davranış aynı mantıktadır. Salt-okuma;
hiçbir emir göndermez. ``python -m app.services.shadow_exit`` ile çalıştırılır.

Sınırlama (v1): her gözlemde ``last_price`` (yoksa ``close``) tek fiyat
noktası olarak kullanılır; bar-içi high/low ekstremumları modellenmez.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.db import MarketObservation, PositionLifecycle
from app.services.exit_policy import DEFAULT_EXIT_POLICY, ExitPolicy
from app.services.performance_report import range_start
from app.services.position_exit_monitor import evaluate_exit

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShadowExitResult:
    reason: str
    exit_r: float
    held_minutes: float
    exit_price: Decimal


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def replay_exit_policy(
    policy: ExitPolicy,
    *,
    entry: Decimal,
    stop: Decimal,
    opened_at: datetime,
    observations: Sequence[MarketObservation],
) -> ShadowExitResult | None:
    """Bir politikayı tek bir girişin gözlem serisi üzerinde oynat.

    İlk tetikte çıkışı döndürür; seri boyunca tetik olmazsa son gözlemde
    ``NO_TRIGGER`` ile kapatır. Geçersiz risk (entry<=stop) ya da kullanılabilir
    fiyat yoksa None.
    """
    risk = entry - stop
    if risk <= 0:
        return None

    opened = _aware(opened_at)
    peak_r = 0.0
    last: ShadowExitResult | None = None
    for obs in observations:
        price = obs.last_price if obs.last_price is not None else obs.close
        if price is None or price <= 0:
            continue
        held_minutes = (_aware(obs.observed_at) - opened).total_seconds() / 60
        current_r = float((price - entry) / risk)
        peak_r = max(peak_r, current_r)
        last = ShadowExitResult("NO_TRIGGER", current_r, held_minutes, price)
        trigger = evaluate_exit(
            policy,
            entry=entry,
            stop=stop,
            best_bid=price,
            peak_r=peak_r,
            held_minutes=held_minutes,
        )
        if trigger is not None:
            return ShadowExitResult(trigger.reason, current_r, held_minutes, price)
    return last


def _aggregate(results: list[ShadowExitResult]) -> dict[str, Any]:
    if not results:
        return {
            "sampleSize": 0,
            "avgExitR": None,
            "winRate": None,
            "avgHoldMinutes": None,
            "reasonBreakdown": {},
        }
    wins = sum(1 for r in results if r.exit_r > 0)
    reasons: dict[str, int] = {}
    for r in results:
        reasons[r.reason] = reasons.get(r.reason, 0) + 1
    return {
        "sampleSize": len(results),
        "avgExitR": round(sum(r.exit_r for r in results) / len(results), 4),
        "winRate": round(wins / len(results) * 100, 2),
        "avgHoldMinutes": round(
            sum(r.held_minutes for r in results) / len(results), 1
        ),
        "reasonBreakdown": reasons,
    }


async def build_shadow_exit_report(
    range_value: str = "7d",
    policies: Sequence[ExitPolicy] | None = None,
) -> dict[str, Any]:
    """Aday politikaları aynı gerçek girişler üzerinde shadow değerlendir.

    Her kapanmış lifecycle için (giriş+stop+gözlem varsa) her politikayı
    oynatır ve politika başına toplam istatistik üretir. ``actual`` bölümü,
    gerçekleşen net P&L işaretinden kaba bir referans kazanma oranı verir.
    """
    policies = list(policies) if policies is not None else [DEFAULT_EXIT_POLICY]
    since = range_start(range_value)

    async with async_session_factory() as session:
        stmt = select(PositionLifecycle).where(PositionLifecycle.status == "CLOSED")
        if since is not None:
            stmt = stmt.where(PositionLifecycle.closed_at >= since)
        lifecycles = list((await session.execute(stmt)).scalars().all())

        by_policy: dict[str, list[ShadowExitResult]] = {p.version: [] for p in policies}
        actual_total = 0
        actual_wins = 0
        evaluated = 0

        for lc in lifecycles:
            entry = lc.average_entry_price
            stop = lc.initial_stop_loss or lc.active_stop_loss
            if entry is None or stop is None or lc.opened_at is None:
                continue
            obs = list(
                (
                    await session.execute(
                        select(MarketObservation)
                        .where(
                            MarketObservation.symbol == lc.symbol,
                            MarketObservation.observed_at >= lc.opened_at,
                            MarketObservation.observed_at
                            <= (lc.closed_at or datetime.now(timezone.utc)),
                        )
                        .order_by(MarketObservation.observed_at.asc())
                    )
                )
                .scalars()
                .all()
            )
            if not obs:
                continue
            evaluated += 1
            actual_total += 1
            if (lc.net_realized_pnl_tl or Decimal("0")) > 0:
                actual_wins += 1
            for policy in policies:
                result = replay_exit_policy(
                    policy,
                    entry=entry,
                    stop=stop,
                    opened_at=lc.opened_at,
                    observations=obs,
                )
                if result is not None:
                    by_policy[policy.version].append(result)

    return {
        "range": range_value,
        "evaluatedEntries": evaluated,
        "actual": {
            "sampleSize": actual_total,
            "winRate": round(actual_wins / actual_total * 100, 2)
            if actual_total
            else None,
        },
        "policies": {version: _aggregate(results) for version, results in by_policy.items()},
    }


def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    # Baseline ile birkaç varyantı aynı girişlerde karşılaştır.
    candidates = [
        DEFAULT_EXIT_POLICY,
        dataclasses.replace(
            DEFAULT_EXIT_POLICY, version="shadow-tight-trailing", trailing_distance_r=0.4
        ),
        dataclasses.replace(
            DEFAULT_EXIT_POLICY, version="shadow-high-target", hard_target_r=2.2
        ),
    ]
    report = asyncio.run(build_shadow_exit_report("30d", candidates))
    logger.info("SHADOW_EXIT_REPORT %s", report)


if __name__ == "__main__":
    _main()
