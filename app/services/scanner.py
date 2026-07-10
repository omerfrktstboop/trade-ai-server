"""Background symbol scanner — eski bot'un OnTimer/ScanDueSymbols döngüsünün
server tarafındaki karşılığı (full-inversion Phase 2).

Lifespan'de başlar, her tick'te (default 60 sn):

1. ``SCANNER_ENABLED`` kapalıysa hiç başlamaz.
2. Kill switch açıksa turu atlar (AI çağrısı ve karar üretimi yok).
3. İşlem kesim saati (cutoff) geçtiyse turu atlar.
4. Gateway'e ulaşılamıyorsa (Matriks kapalı) turu atlar — hata fırlatmaz.
5. Sırası gelen sembolleri (scan interval dolmuş VEYA admin pending override'ı
   olan) ``evaluator.evaluate_symbol`` ile değerlendirir.

Emir yolu (Phase 2): ``SCANNER_ALLOW_ORDERS=false`` (default) iken tüm
kararlar PAPER'a sabitlenir — Phase 1 davranışının aynısı. ``true`` iken mod
admin panelin ``tradingMode`` override'ından gelir ve yalnızca **DEMO_LIVE**
kararları gateway'e emir olarak gönderilir; REAL_LIVE/LIVE bu fazda kod
seviyesinde bloklu. Senkron emir sonuçları ``order_logs``'a yazılır; nihai
borsa durumu gateway'in OnOrderUpdate → /api/order-result raporuyla gelir.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.core.risk_config import risk_config
from app.db.session import async_session_factory
from app.models.db import OrderLog
from app.models.signal import OrderType, SignalAction, SignalMode
from app.services.admin_config import build_runtime_risk_config, is_kill_switch_enabled
from app.services.evaluator import EvaluationResult, evaluate_symbol
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    MatriksGatewayClient,
    gateway_client,
)
from app.services.position_sync import sync_positions_from_gateway
from app.services.signal_override import list_pending_override_symbols
from app.services.trade_profile import get_active_profile

logger = logging.getLogger(__name__)

# Aynı uyarıyı her tick'te loglamamak için susturma süresi.
_WARN_SUPPRESS = timedelta(minutes=5)


class SymbolScanner:
    """Tek instance'lık arka plan tarayıcı. ``start()``/``stop()`` lifespan'den çağrılır."""

    def __init__(
        self,
        gateway: MatriksGatewayClient | None = None,
        tick_seconds: float | None = None,
    ) -> None:
        self._gateway = gateway or gateway_client
        self._tick_seconds = tick_seconds or settings.scanner_tick_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._last_scan_by_symbol: dict[str, datetime] = {}
        self._last_warn_by_key: dict[str, datetime] = {}

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="symbol-scanner")
        logger.info(
            "Scanner started tick=%ss (Phase 1: PAPER-only, no order path)",
            self._tick_seconds,
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None
        logger.info("Scanner stopped.")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ── Loop ───────────────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception:
                # Tek bir tick hatası döngüyü asla öldürmemeli.
                logger.exception("Scanner tick failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._tick_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def tick(self) -> list[str]:
        """Tek tarama turu. Değerlendirilen sembollerin listesini döndürür (test için)."""
        # ── Runtime config (kill switch, cutoff, semboller, interval) ──────
        kill_switch = False
        runtime_cfg = risk_config
        scan_interval_minutes = 30
        pending_overrides: set[str] = set()
        try:
            async with async_session_factory() as session:
                kill_switch = await is_kill_switch_enabled(session)
                runtime_cfg = await build_runtime_risk_config(session)
                profile = await get_active_profile(session)
                scan_interval_minutes = int(profile.scan_interval_minutes)
                pending_overrides = {
                    s.strip().upper()
                    for s in await list_pending_override_symbols(session)
                }
        except Exception:
            self._warn_throttled(
                "config", "Runtime config unavailable; using static .env defaults"
            )

        if kill_switch:
            self._warn_throttled("killswitch", "Kill switch enabled; skipping scan cycle")
            return []

        if not runtime_cfg.can_trade_now():
            self._warn_throttled(
                "cutoff",
                f"Trading cutoff passed ({runtime_cfg.disable_trading_after} "
                f"{runtime_cfg.timezone}); skipping scan cycle",
            )
            return []

        # ── Gateway sağlık kontrolü — Matriks kapalıysa tur atlanır ────────
        if not await self._gateway.is_available():
            self._warn_throttled(
                "gateway", "Matriks gateway unavailable; skipping scan cycle"
            )
            return []

        # ── Pozisyonları gateway'den tazele ────────────────────────────────
        # Admin panelinin Positions sayfası ve acil "tümünü sat" akışı
        # bot_positions'tan okuyor; eski push endpoint'i kaldırıldığı için
        # bu tabloyu güncel tutmak scanner'ın sorumluluğunda.
        await sync_positions_from_gateway(self._gateway)

        # ── Sırası gelen sembolleri değerlendir ────────────────────────────
        symbols = [
            s.strip().upper()
            for s in runtime_cfg.allowed_symbols.split(",")
            if s.strip()
        ]
        interval = timedelta(minutes=max(1, scan_interval_minutes))
        now = datetime.now(timezone.utc)

        evaluated: list[str] = []
        for symbol in symbols:
            last_scan = self._last_scan_by_symbol.get(symbol)
            due = last_scan is None or (now - last_scan) >= interval
            if not due and symbol not in pending_overrides:
                continue

            try:
                # SCANNER_ALLOW_ORDERS=false → PAPER'a sabit (Phase 1 davranışı).
                result = await evaluate_symbol(
                    symbol, force_paper=not settings.scanner_allow_orders
                )
            except GatewayUnavailable:
                self._warn_throttled(
                    "gateway", "Gateway became unavailable mid-cycle; stopping this tick"
                )
                break
            except GatewayError as exc:
                logger.warning("Snapshot rejected by gateway symbol=%s error=%s", symbol, exc)
                self._last_scan_by_symbol[symbol] = now
                continue
            except Exception:
                logger.exception("Evaluation failed symbol=%s", symbol)
                self._last_scan_by_symbol[symbol] = now
                continue

            self._last_scan_by_symbol[symbol] = now
            evaluated.append(symbol)
            if result is None:
                logger.info("Scan skipped (no usable price) symbol=%s", symbol)
                continue

            response = result.response
            logger.info(
                "Scan decision symbol=%s action=%s confidence=%s allowOrder=%s mode=%s",
                symbol,
                response.action.value,
                response.confidence_score,
                response.allow_order,
                result.mode.value,
            )
            await self._maybe_send_order(result)

        return evaluated

    # ── Order path (Phase 2) ───────────────────────────────────────────────

    async def _maybe_send_order(self, result: EvaluationResult) -> None:
        """Karar emre dönüşmeli mi? Tüm kapılar geçerse gateway'e gönder.

        Buradaki kapılar ilk savunma hattı; gateway (C#) aynı kontrolleri
        kendi sabit limitleriyle bir kez daha uygular.
        """
        if not settings.scanner_allow_orders:
            return

        response = result.response
        if not response.allow_order or response.requires_confirmation:
            return
        if response.action not in (SignalAction.BUY, SignalAction.SELL):
            return
        if response.order_type != OrderType.LIMIT:
            logger.warning(
                "Order skipped: non-LIMIT orderType=%s requestId=%s",
                response.order_type.value,
                response.request_id,
            )
            return
        if response.qty <= 0 or not response.price or response.price <= 0:
            logger.warning(
                "Order skipped: invalid qty/price qty=%s price=%s requestId=%s",
                response.qty,
                response.price,
                response.request_id,
            )
            return

        # Phase 2 kapısı: sadece DEMO_LIVE emre dönüşür.
        if result.mode != SignalMode.DEMO_LIVE:
            logger.warning(
                "Order blocked: mode=%s is not allowed to send orders in Phase 2 "
                "(only DEMO_LIVE) requestId=%s",
                result.mode.value,
                response.request_id,
            )
            return

        try:
            outcome = await self._gateway.send_order(
                request_id=response.request_id,
                symbol=response.symbol,
                side=response.action.value,
                qty=response.qty,
                limit_price=response.price,
                mode=result.mode.value,
            )
            status = str(outcome.get("status", "UNKNOWN"))
            reason = str(outcome.get("reason", ""))
            logger.info(
                "Order %s symbol=%s side=%s qty=%s price=%s requestId=%s reason=%s",
                status,
                response.symbol,
                response.action.value,
                response.qty,
                response.price,
                response.request_id,
                reason,
            )
        except (GatewayUnavailable, GatewayError) as exc:
            status = "ERROR"
            reason = str(exc)
            logger.error(
                "Order send failed symbol=%s requestId=%s error=%s",
                response.symbol,
                response.request_id,
                exc,
            )

        await self._persist_order_outcome(response, status, reason)

    async def _persist_order_outcome(self, response, status: str, reason: str) -> None:
        """Senkron emir sonucunu order_logs'a yaz.

        Gateway'in OnOrderUpdate raporu /api/order-result üzerinden ayrıca
        gelir; bu kayıt gönderim anındaki sonucu (SENT_PENDING/REJECTED/ERROR)
        tutar ki reddedilen emirler de izlenebilsin.
        """
        try:
            async with async_session_factory() as session:
                session.add(
                    OrderLog(
                        request_id=response.request_id,
                        symbol=response.symbol,
                        action=response.action.value,
                        qty=response.qty,
                        price=response.price or 0.0,
                        status=status,
                        order_id=None,
                        matrix_message=f"scanner: {reason}",
                    )
                )
                await session.commit()
        except Exception:
            logger.exception(
                "Failed to persist order outcome requestId=%s", response.request_id
            )

    # ── Helpers ────────────────────────────────────────────────────────────

    def _warn_throttled(self, key: str, message: str) -> None:
        now = datetime.now(timezone.utc)
        last = self._last_warn_by_key.get(key)
        if last is not None and (now - last) < _WARN_SUPPRESS:
            return
        self._last_warn_by_key[key] = now
        logger.warning(message)


# Lifespan'in kullandığı paylaşılan instance.
scanner = SymbolScanner()
