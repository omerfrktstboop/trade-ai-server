"""Bağımsız deterministik exit monitörü (Plan Faz 2.1).

Çıkış kontrolü scanner/discovery/LLM işlerinden ayrılır ve kendi hızlı
cadence'inde (5-10 sn) çalışır: açık her bot pozisyonu için taze **best bid**
üzerinden R-bazlı politikayı (``exit_policy``) uygular, tetiklenirse bir
``ExitIntent`` yazar ve çıkışı mevcut ``_maybe_send_order`` dispatch yoluyla
gönderir — böylece kill switch, cutoff, ownership, preflight ve gateway sert
kapları aynen geçerli kalır.

Tek yetki kuralı (plan bölüm 6): bu monitör yalnızca
``deterministic_exit_enabled`` açıkken başlar; o durumda scanner'ın
tick'e bağlı ``stop_loss_guard``'ı devre dışı bırakılır. İki bağımsız otorite
aynı pozisyonu kapatmaya çalışamaz.

Peak/MFE durumu süreç-içi tutulur (restart'ta sıfırlanır): restart sonrası
break-even/trailing yeniden aktive olmayı bekler — asla yanlış-yön üretmez,
yalnızca o an biraz daha az koruyucudur; stop ve max-hold mutlaktır.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Awaitable, Callable

from app.config import settings
from app.db.session import async_session_factory
from app.models.db import PositionLifecycle
from app.models.signal import OrderType, SignalAction, SignalResponse
from app.services.evaluation import EvaluationResult
from app.services.exit_policy import (
    ExitPolicy,
    get_active_exit_policy,
    record_exit_intent,
)
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    MatriksGatewayClient,
    gateway_client,
)

logger = logging.getLogger(__name__)

DispatchCallback = Callable[[EvaluationResult], Awaitable[None]]


@dataclass(frozen=True)
class ExitTrigger:
    reason: str
    trigger_price: Decimal


def evaluate_exit(
    policy: ExitPolicy,
    *,
    entry: Decimal,
    stop: Decimal,
    best_bid: Decimal,
    peak_r: float,
    held_minutes: float,
) -> ExitTrigger | None:
    """Bir açık pozisyon için deterministik çıkış kararı (saf, yan etkisiz).

    R = (best_bid − entry) / (entry − stop). Öncelik: STOP > HARD_TARGET >
    TRAILING > BREAKEVEN > STAGNATION > MAX_HOLD. Tetik yoksa None.
    ``peak_r`` bu pozisyonun şimdiye kadarki en yüksek R'sidir (MFE).
    """
    risk = entry - stop
    if risk <= 0:
        # Geçersiz seviye: yalnızca mutlak stop ve max-hold uygulanabilir.
        if best_bid <= stop:
            return ExitTrigger("STOP", best_bid)
        if held_minutes >= policy.max_holding_minutes:
            return ExitTrigger("MAX_HOLD", best_bid)
        return None

    current_r = float((best_bid - entry) / risk)

    # Mutlak stop — her şeyin önünde.
    if best_bid <= stop:
        return ExitTrigger("STOP", best_bid)

    # Sert hedef.
    if current_r >= policy.hard_target_r:
        return ExitTrigger("HARD_TARGET", best_bid)

    # Trailing: aktive olduysa tepe R'den geri çekilme.
    if peak_r >= policy.trailing_activation_r:
        trailing_stop_r = peak_r - policy.trailing_distance_r
        if current_r <= trailing_stop_r:
            return ExitTrigger("TRAILING", best_bid)

    # Break-even: aktive olduysa maliyet seviyesine (entry) dönüş.
    if peak_r >= policy.breakeven_activation_r and current_r <= 0:
        return ExitTrigger("BREAKEVEN", best_bid)

    # Durgunluk: yeterince beklenmiş ve MFE hâlâ tavan altında.
    if (
        held_minutes >= policy.stagnation_minutes
        and peak_r < policy.stagnation_mfe_r_ceiling
    ):
        return ExitTrigger("STAGNATION", best_bid)

    # Maksimum tutma süresi.
    if held_minutes >= policy.max_holding_minutes:
        return ExitTrigger("MAX_HOLD", best_bid)

    return None


def _best_bid(payload: dict) -> Decimal | None:
    for key in ("bestBid", "bidPrice"):
        value = payload.get(key)
        if value is not None:
            try:
                bid = Decimal(str(value))
            except (ValueError, ArithmeticError):
                continue
            if bid > 0:
                return bid
    return None


class PositionExitMonitor:
    """Açık pozisyonları bağımsız, hızlı cadence'de izleyip çıkış gönderir."""

    def __init__(
        self,
        *,
        gateway: MatriksGatewayClient = gateway_client,
        interval_seconds: float | None = None,
    ) -> None:
        self._gateway = gateway
        self._interval_seconds = max(
            3.0, interval_seconds or settings.exit_monitor_interval_seconds
        )
        self._dispatch: DispatchCallback | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._peak_r: dict[int, float] = {}

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def set_dispatch(self, dispatch: DispatchCallback) -> None:
        self._dispatch = dispatch

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="position-exit-monitor")
        logger.info(
            "Position exit monitor started interval=%ss", self._interval_seconds
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None
        logger.info("Position exit monitor stopped")

    async def tick_once(self) -> int:
        """Bir tur: açık pozisyonları değerlendir, tetiklenenlerin çıkışını
        gönder. Döndürdüğü sayı gönderilen çıkış sayısıdır. İstisna yutulur."""
        try:
            return await self._tick_once()
        except Exception:
            logger.exception("Position exit monitor tick failed")
            return 0

    async def _tick_once(self) -> int:
        policy = get_active_exit_policy()

        # Faz 2.3: dolmayan stale çıkış emirlerini iptal et (urgency'ye göre).
        # İptal edilen semboller bu tur yeniden değerlendirilmez — cancel
        # gateway'de otururken çift SELL göndermemek için; bir sonraki tur taze
        # best bid'den yeniden tetikler.
        repriced_symbols = await self._reprice_stale_intents(policy)

        async with async_session_factory() as session:
            from sqlalchemy import select

            open_positions = list(
                (
                    await session.execute(
                        select(PositionLifecycle).where(
                            PositionLifecycle.status == "OPEN"
                        )
                    )
                )
                .scalars()
                .all()
            )

        live_ids = {lc.id for lc in open_positions}
        # Kapanan pozisyonların peak durumunu temizle.
        for stale_id in [pid for pid in self._peak_r if pid not in live_ids]:
            self._peak_r.pop(stale_id, None)

        dispatched = 0
        for lc in open_positions:
            if lc.symbol.upper() in repriced_symbols:
                continue
            trigger = await self._evaluate_position(lc, policy)
            if trigger is None:
                continue
            if await self._dispatch_exit(lc, trigger, policy):
                dispatched += 1
        return dispatched

    async def _evaluate_position(
        self, lc: PositionLifecycle, policy: ExitPolicy
    ) -> ExitTrigger | None:
        entry = lc.average_entry_price
        stop = lc.active_stop_loss or lc.initial_stop_loss
        qty = lc.current_qty or Decimal("0")
        if entry is None or stop is None or qty <= 0:
            return None

        try:
            snapshot = await self._gateway.get_snapshot(lc.symbol)
        except (GatewayUnavailable, GatewayError) as exc:
            logger.warning("Exit monitor snapshot failed symbol=%s %s", lc.symbol, exc)
            return None
        payload = snapshot.get("payload", snapshot) if isinstance(snapshot, dict) else {}
        best_bid = _best_bid(payload) if isinstance(payload, dict) else None
        if best_bid is None:
            return None

        risk = entry - stop
        current_r = float((best_bid - entry) / risk) if risk > 0 else 0.0
        prior_peak = self._peak_r.get(lc.id, 0.0)
        peak_r = max(prior_peak, current_r)
        self._peak_r[lc.id] = peak_r

        opened = lc.opened_at
        if opened is not None and opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        held_minutes = (
            (datetime.now(timezone.utc) - opened).total_seconds() / 60
            if opened is not None
            else 0.0
        )
        return evaluate_exit(
            policy,
            entry=entry,
            stop=stop,
            best_bid=best_bid,
            peak_r=peak_r,
            held_minutes=held_minutes,
        )

    async def _dispatch_exit(
        self, lc: PositionLifecycle, trigger: ExitTrigger, policy: ExitPolicy
    ) -> bool:
        if self._dispatch is None:
            logger.error("Exit monitor has no dispatch callback; cannot exit %s", lc.symbol)
            return False

        sell_qty = int(lc.current_qty or 0)
        if sell_qty <= 0:
            return False
        request_id = (
            f"{lc.symbol}-exit-{trigger.reason}-"
            f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        )

        async with async_session_factory() as session:
            await record_exit_intent(
                session,
                symbol=lc.symbol,
                exit_reason=trigger.reason,
                trigger_price=trigger.trigger_price,
                policy_version=policy.version,
                position_lifecycle_id=lc.id,
                request_id=request_id,
                status="ACCEPTED",
            )
            await session.commit()

        # Çıkış marketable-fakat-korumalı LIMIT: tetikleyen best bid'den ver.
        response = SignalResponse(
            requestId=request_id,
            symbol=lc.symbol,
            action=SignalAction.SELL,
            qty=sell_qty,
            orderType=OrderType.LIMIT,
            price=trigger.trigger_price,
            confidenceScore=100.0,
            riskScore=100.0,
            allowOrder=True,
            reason=(
                f"Deterministic exit monitor {trigger.reason} "
                f"(policy {policy.version}); independent of AI"
            ),
            entryRange=None,
            stopLoss=lc.active_stop_loss or lc.initial_stop_loss,
            targetPrice=lc.active_target_price or lc.initial_target_price,
        )
        result = EvaluationResult(
            response=response,
            dispatch_eligible=True,
            evaluation_purpose="POSITION_EXIT_MONITOR",
            decision_entry_price=lc.average_entry_price,
            decision_target_price=lc.active_target_price or lc.initial_target_price,
        )
        logger.warning(
            "EXIT_MONITOR_TRIGGERED symbol=%s reason=%s bid=%s qty=%s",
            lc.symbol,
            trigger.reason,
            trigger.trigger_price,
            sell_qty,
        )
        await self._dispatch(result)
        return True

    async def _reprice_stale_intents(self, policy: ExitPolicy) -> set[str]:
        """Dolmayan stale çıkış emirlerini iptal et; iptal edilen sembolleri döndür.

        Acil çıkışlar (stop/breakeven/stagnation/max-hold) ``urgent_reprice``,
        pasif kâr-al (hard target/trailing) ``passive_reprice`` penceresinden
        sonra reprice edilir. İptal edilen ExitIntent CANCELED işaretlenir ve
        pozisyon açık kaldığı için bir sonraki tur taze fiyattan yeniden
        tetiklenir (cancel-and-re-fire).
        """
        from sqlalchemy import select

        from app.models.db import OrderLog
        from app.services.exit_policy import (
            open_exit_intents,
            update_exit_intent_status,
        )

        urgent_reasons = {"STOP", "BREAKEVEN", "STAGNATION", "MAX_HOLD"}
        now = datetime.now(timezone.utc)
        repriced: set[str] = set()
        async with async_session_factory() as session:
            intents = await open_exit_intents(session)
            for intent in intents:
                trig = intent.trigger_at
                if trig.tzinfo is None:
                    trig = trig.replace(tzinfo=timezone.utc)
                window = (
                    policy.urgent_reprice_seconds
                    if intent.exit_reason in urgent_reasons
                    else policy.passive_reprice_seconds
                )
                if (now - trig).total_seconds() < window:
                    continue

                order = (
                    await session.execute(
                        select(OrderLog).where(OrderLog.request_id == intent.request_id)
                    )
                ).scalar_one_or_none()
                if order is None:
                    # Emre hiç dönüşmemiş (dispatch kapıları reddetmiş); niyeti
                    # serbest bırak ki açık pozisyon yeniden tetiklenebilsin.
                    await update_exit_intent_status(session, intent, status="FAILED")
                    continue

                status = (order.status or "").upper()
                filled = bool(
                    order.filled_qty
                    and order.order_qty
                    and order.filled_qty >= order.order_qty
                )
                if status == "FILLED" or filled:
                    await update_exit_intent_status(
                        session, intent, status="FILLED", order_id=order.order_id
                    )
                    continue
                if status in {"REJECTED", "ERROR", "CANCELED"}:
                    await update_exit_intent_status(
                        session, intent, status="FAILED", order_id=order.order_id
                    )
                    continue

                # Hâlâ bekliyor ve stale → iptal et, yeniden tetiklenmeye bırak.
                if order.order_id:
                    try:
                        await self._gateway.cancel_order(order.order_id)
                    except (GatewayUnavailable, GatewayError) as exc:
                        logger.warning(
                            "Exit reprice cancel failed symbol=%s orderId=%s %s",
                            intent.symbol,
                            order.order_id,
                            exc,
                        )
                        continue
                await update_exit_intent_status(
                    session,
                    intent,
                    status="CANCELED",
                    order_id=order.order_id,
                    bump_generation=True,
                )
                repriced.add(intent.symbol.upper())
                logger.warning(
                    "EXIT_INTENT_REPRICED symbol=%s reason=%s ageSec=%.0f",
                    intent.symbol,
                    intent.exit_reason,
                    (now - trig).total_seconds(),
                )
            await session.commit()
        return repriced

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            await self.tick_once()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_seconds
                )
            except asyncio.TimeoutError:
                pass


position_exit_monitor = PositionExitMonitor()
