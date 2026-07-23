"""Genel amaçlı periyodik arka plan worker'ı.

``PositionSynchronizer``/``OrderSynchronizer`` her biri kendi start/stop/loop
boilerplate'ini taşıyordu. Ölçüm hattı worker'ları (outcome labeler, measurement
reconciliation) yalnızca "her N saniyede bir şu ``run_once()`` coroutine'ini
çağır, hata olursa yut ve devam et" istiyor — bu tekrarı tek yerde toplar.

Sözleşme:
- ``run_once`` argümansız bir async callable'dır; dönüş değeri log dışında
  kullanılmaz.
- Bir tick içindeki istisna yakalanır, loglanır ve döngü bir sonraki tick'e
  devam eder — tek bir hatalı çalışma worker'ı öldürmez.
- ``stop()`` bekleyen uyku süresini beklemeden döngüyü nazikçe sonlandırır.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


class PeriodicWorker:
    """Bir async ``run_once`` callable'ını sabit aralıkla çalıştırır."""

    def __init__(
        self,
        *,
        name: str,
        run_once: Callable[[], Awaitable[object]],
        interval_seconds: float,
    ) -> None:
        self._name = name
        self._run_once = run_once
        self._interval_seconds = max(5.0, float(interval_seconds))
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._last_attempt_at: datetime | None = None
        self._last_completed_at: datetime | None = None
        self._last_error: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def get_status(self) -> dict[str, object]:
        return {
            "name": self._name,
            "running": self.running,
            "intervalSeconds": self._interval_seconds,
            "lastAttemptAt": self._last_attempt_at.isoformat()
            if self._last_attempt_at
            else None,
            "lastCompletedAt": self._last_completed_at.isoformat()
            if self._last_completed_at
            else None,
            "lastError": self._last_error,
        }

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name=self._name)
        logger.info(
            "%s started interval=%ss", self._name, self._interval_seconds
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None
        logger.info("%s stopped", self._name)

    async def tick_once(self) -> None:
        """Tek bir çalışma; istisnayı yutar (döngü ile aynı davranış)."""
        self._last_attempt_at = datetime.now(timezone.utc)
        try:
            await self._run_once()
        except Exception as exc:  # noqa: BLE001 - worker tek hatada ölmemeli
            self._last_error = str(exc)
            logger.exception("%s tick failed", self._name)
            return
        self._last_completed_at = datetime.now(timezone.utc)
        self._last_error = None

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            await self.tick_once()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_seconds
                )
            except asyncio.TimeoutError:
                pass
