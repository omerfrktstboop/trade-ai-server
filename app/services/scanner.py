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

from sqlalchemy import select

from app.config import settings
from app.core.risk_config import risk_config
from app.db.session import async_session_factory
from app.models.db import BotPosition, OrderLog
from app.models.signal import OrderType, SignalAction, SignalMode
from app.services.admin_config import (
    build_runtime_risk_config,
    is_kill_switch_enabled,
)
from app.services.discovery_agent import (
    list_active_watchlist_symbols,
    run_discovery_scan,
)
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
_ORDER_COOLDOWN = timedelta(minutes=15)


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
        self._last_order_sent_at: dict[tuple[str, SignalAction], datetime] = {}
        self._last_discovery_by_symbol: dict[str, datetime] = {}
        self._last_discovery_run: datetime | None = None
        self._last_portfolio_scan: datetime | None = None
        self._last_tick_at: datetime | None = None
        self._last_evaluated_symbols: list[str] = []

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

    def get_status(self) -> dict[str, object]:
        """Return a read-only runtime snapshot for the admin status view."""
        return {
            "enabled": settings.scanner_enabled,
            "allowOrders": settings.scanner_allow_orders,
            "running": self.running,
            "tickSeconds": self._tick_seconds,
            "lastTickAt": self._last_tick_at.isoformat() if self._last_tick_at else None,
            "lastEvaluatedSymbols": list(self._last_evaluated_symbols),
            "lastDiscoveryRunAt": (
                self._last_discovery_run.isoformat() if self._last_discovery_run else None
            ),
            "lastPortfolioScanAt": (
                self._last_portfolio_scan.isoformat() if self._last_portfolio_scan else None
            ),
        }

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
        self._last_tick_at = datetime.now(timezone.utc)
        self._last_evaluated_symbols = []
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
        try:
            gateway_health = await self._gateway.health()
        except (GatewayUnavailable, GatewayError):
            self._warn_throttled(
                "gateway", "Matriks gateway unavailable; skipping scan cycle"
            )
            return []
        if not gateway_health.get("positionsLoaded"):
            self._warn_throttled(
                "positions",
                "Matriks positions are not loaded; skipping scan cycle",
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
        # Discovery agent'ın bulduğu aktif watchlist adayları da taranır —
        # emir izni yine RiskEngine'in allowedSymbols kontrolünde kalır.
        watchlist = await list_active_watchlist_symbols()
        symbols.extend(s for s in watchlist if s not in symbols)
        # Manual BUY/SELL overrides must run even when the symbol is not in
        # the regular scan watchlist (for example an existing portfolio
        # position such as OPT25F). Preserve watchlist order, then append the
        # pending symbols deterministically.
        symbols.extend(sorted(pending_overrides.difference(symbols)))
        interval = timedelta(minutes=max(1, scan_interval_minutes))
        now = datetime.now(timezone.utc)

        evaluated: list[str] = []
        gateway_down_mid_cycle = False
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
                gateway_down_mid_cycle = True
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

        # ── Otonom keşif (movers → watchlist) + portföy re-evaluasyonu ────
        # Gateway tur ortasında düştüyse aynı tick'te tekrar denemek anlamsız.
        if not gateway_down_mid_cycle:
            await self._run_discovery()
            await self._run_portfolio_scan(pending_overrides)

        self._last_evaluated_symbols = list(evaluated)
        return evaluated

    async def _run_discovery(self) -> None:
        """Discovery agent'ı periyodik çalıştır: movers → elemeler → watchlist.

        Adayları ``watchlist_symbols`` tablosuna yazar; bir sonraki tick'te
        tarama listesine otomatik girerler. LLM çağrısı YAPMAZ — eleme
        tamamen kural tabanlı (tavan kilidi / sığ hacim / satış duvarı).
        """
        interval = timedelta(minutes=max(5, settings.discovery_interval_minutes))
        now = datetime.now(timezone.utc)
        if self._last_discovery_run and (now - self._last_discovery_run) < interval:
            return
        self._last_discovery_run = now

        try:
            added = await run_discovery_scan(self._gateway)
        except Exception:
            logger.exception("Discovery scan failed")
            return
        if added:
            logger.info("Discovery scan accepted symbols=%s", added)

    async def _run_portfolio_scan(self, pending_overrides: set[str]) -> None:
        """Eldeki pozisyonları periyodik yeniden değerlendir (Portfolio Manager).

        ``bot_positions``ta lot bulunan her sembol LLM'e pozisyon bağlamıyla
        gider (evaluator ``positionContext`` ekler; prompt kural 16: kar al /
        zarar kes / tut). Normal tarama zaten pozisyonlu sembolleri kapsıyor
        olabilir — bu döngü, izleme listesinden çıkmış (ör. watchlist'ten
        alınmış sonra pasifleşmiş) pozisyonların da yönetimsiz kalmamasını
        garanti eder.
        """
        interval = timedelta(minutes=max(5, settings.portfolio_scan_interval_minutes))
        now = datetime.now(timezone.utc)
        if self._last_portfolio_scan and (now - self._last_portfolio_scan) < interval:
            return
        self._last_portfolio_scan = now

        try:
            async with async_session_factory() as session:
                rows = (
                    await session.execute(
                        select(BotPosition).where(BotPosition.qty > 0)
                    )
                ).scalars().all()
        except Exception:
            logger.exception("Portfolio scan: bot_positions read failed")
            return

        held = [row.symbol.strip().upper() for row in rows]
        if not held:
            return

        logger.info("Portfolio scan starting positions=%s", held)
        for symbol in held:
            if self._stop_event.is_set():
                break
            # Normal tarama bu sembolü zaten yakın zamanda değerlendirdiyse
            # (aynı tick dahil) tekrarlamak sadece token yakar — atla.
            last_scan = self._last_scan_by_symbol.get(symbol)
            if last_scan is not None and (now - last_scan) < interval:
                continue
            try:
                result = await evaluate_symbol(
                    symbol, force_paper=not settings.scanner_allow_orders
                )
            except GatewayUnavailable:
                self._warn_throttled(
                    "gateway", "Gateway unavailable during portfolio scan; stopping"
                )
                break
            except GatewayError as exc:
                logger.warning(
                    "Portfolio scan snapshot rejected symbol=%s error=%s", symbol, exc
                )
                continue
            except Exception:
                logger.exception("Portfolio scan evaluation failed symbol=%s", symbol)
                continue

            if result is None:
                continue

            # Normal tarama zamanlayıcısını da tazele — aynı tick içinde
            # sembol ikinci kez değerlendirilmesin.
            self._last_scan_by_symbol[symbol] = now
            response = result.response
            logger.info(
                "Portfolio decision symbol=%s action=%s confidence=%s allowOrder=%s",
                symbol,
                response.action.value,
                response.confidence_score,
                response.allow_order,
            )
            await self._maybe_send_order(result)

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

        cooldown_key = (response.symbol.strip().upper(), response.action)
        last_sent = self._last_order_sent_at.get(cooldown_key)
        now = datetime.now(timezone.utc)
        if last_sent is None:
            try:
                async with async_session_factory() as session:
                    stmt = (
                        select(OrderLog.created_at)
                        .where(
                            OrderLog.symbol == cooldown_key[0],
                            OrderLog.action == response.action.value,
                            OrderLog.status.in_(
                                ("SENT_PENDING", "NEW", "A", "PARTIALLY_FILLED", "FILLED")
                            ),
                            OrderLog.created_at >= now - _ORDER_COOLDOWN,
                        )
                        .order_by(OrderLog.created_at.desc())
                        .limit(1)
                    )
                    last_sent = (await session.execute(stmt)).scalar_one_or_none()
                if last_sent is not None and last_sent.tzinfo is None:
                    last_sent = last_sent.replace(tzinfo=timezone.utc)
                if last_sent is not None:
                    self._last_order_sent_at[cooldown_key] = last_sent
            except Exception:
                logger.exception(
                    "Failed to read persistent order cooldown symbol=%s side=%s",
                    response.symbol,
                    response.action.value,
                )
        if last_sent is not None and now - last_sent < _ORDER_COOLDOWN:
            remaining = _ORDER_COOLDOWN - (now - last_sent)
            logger.warning(
                "Order skipped: cooldown active symbol=%s side=%s remaining=%ss requestId=%s",
                response.symbol,
                response.action.value,
                max(1, int(remaining.total_seconds())),
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
            if outcome.get("accepted"):
                self._last_order_sent_at[cooldown_key] = now
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
