"""Background symbol scanner - eski bot'un OnTimer/ScanDueSymbols döngüsünün
server tarafındaki karşılığı (full-inversion Phase 2).

Lifespan'de başlar, her tick'te (default 60 sn):

1. ``SCANNER_ENABLED`` kapalıysa hiç başlamaz.
2. Kill switch açıksa turu atlar (AI çağrısı ve karar üretimi yok).
3. Gateway'e ulaşılamıyorsa (Matriks kapalı) turu atlar - hata fırlatmaz.
4. Research discovery, pozisyon anlık görüntüsünden bağımsız market-data ile çalışır.
5. İşlem kesim saati (cutoff) geçmişse veya pozisyonlar yüklenmemişse trade
   değerlendirmesi atlanır.
6. Sırası gelen sembolleri (scan interval dolmuş VEYA admin pending override'ı
   olan) ``evaluator.evaluate_symbol`` ile değerlendirir.

Emir yolu (Phase 2): ``SCANNER_ALLOW_ORDERS=false`` (default) iken tüm
kararlar PAPER'a sabitlenir - Phase 1 davranışının aynısı. ``true`` iken mod
admin panelin ``tradingMode`` override'ından gelir ve yalnızca **DEMO_LIVE**
kararları gateway'e emir olarak gönderilir; REAL_LIVE/LIVE bu fazda kod
seviyesinde bloklu. Senkron emir sonuçları ``order_logs``'a yazılır; nihai
borsa durumu gateway'in OnOrderUpdate -> /api/order-result raporuyla gelir.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from time import monotonic

from sqlalchemy import select

from app.config import settings
from app.core.risk_config import risk_config
from app.db.session import async_session_factory
from app.models.db import (
    BotPosition,
    OrderLog,
    PositionLifecycle,
    PositionStopEvent,
    RiskDecision,
    TradeWatchlistSymbol,
)
from app.models.signal import OrderType, SignalAction, SignalMode
from app.services.admin_config import (
    build_runtime_risk_config,
    get_admin_config_value,
    get_ai_tool_calling_enabled,
    get_portfolio_scan_interval_minutes,
    get_scanner_allow_orders,
    get_system_mode,
    is_kill_switch_enabled,
    is_scanner_runtime_enabled,
)
from app.core.runtime_flags import dispatch_block_reason, is_dispatch_blocked
from app.services.account_watcher import account_watcher
from app.services.ai_provider import get_default_provider
from app.services.discovery_agent import run_discovery_scan
from app.services.evaluator import EvaluationResult, evaluate_symbol
from app.services.account_context import (
    MatriksAccountContextAdapter,
    fetch_fresh_account_inputs,
    get_account_reservation_handling,
)
from app.services.cash_reservation import reserve_sized_buy
from app.services.effective_risk_config import resolve_effective_risk_config
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    MatriksGatewayClient,
    gateway_client,
)
from app.services.notifications import (
    notify_gateway_event,
    notify_order_event,
    notify_risk_block,
)
from app.services.manual_approvals import queue_response
from app.services.order_sync import cancel_timed_out_orders
from app.services.order_ledger import mark_send_result, mark_send_started, reserve_order
from app.services.order_preflight import parse_finite_decimal, validate_order_preflight
from app.services.position_sizing import TradeSizingContext
from app.services.signal_override import list_pending_override_symbols
from app.services.significance import (
    build_observation,
    load_event_fingerprints,
    significance_detector,
)
from app.services.market_observation_collector import market_observation_collector
from app.services.stop_loss_guard import check_stop_loss_positions, stop_loss_guard
from app.services.trade_profile import get_active_profile
from app.services.research_pipeline import (
    get_pipeline_counts,
    is_trade_eligible,
    list_trade_eligible_symbols,
    load_research_policy,
    maintain_trade_watchlist,
    record_trade_watchlist_decision,
    run_research_cycle,
)

logger = logging.getLogger(__name__)


def _configured_default_mode() -> SignalMode:
    """Translate the configured default mode for scanner-originated requests."""
    return SignalMode(settings.default_mode.value.upper())


async def _orders_enabled() -> bool:
    """Emir gönderim ana anahtarı (admin panel > .env varsayılanı).

    DB'ye ulaşılamazsa .env değerine düşer; panelde satır yoksa da .env
    değeri geçerlidir, yani mevcut kurulumlar davranış değiştirmez.
    """
    try:
        async with async_session_factory() as session:
            return await get_scanner_allow_orders(session)
    except Exception:
        return settings.scanner_allow_orders


# Aynı uyarıyı her tick'te loglamamak için susturma süresi.
_WARN_SUPPRESS = timedelta(minutes=5)
_ORDER_COOLDOWN = timedelta(minutes=15)


async def _decision_audit_exists(request_id: str) -> bool:
    """Dispatch öncesi audit doğrulaması (v2 ilke #6).

    Normal değerlendirme yolu ``risk_decisions`` satırı yazar; stop-loss
    bekçisi ise LLM'siz çalıştığı için ``position_stop_events``'e
    STOP_BREACHED olayı yazar. İkisinden biri commit edilmiş olmalı.
    """
    async with async_session_factory() as session:
        risk_row = (
            await session.execute(
                select(RiskDecision.id)
                .where(RiskDecision.request_id == request_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if risk_row is not None:
            return True
        stop_row = (
            await session.execute(
                select(PositionStopEvent.id)
                .where(PositionStopEvent.source_request_id == request_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        return stop_row is not None


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
        self._last_research_run: datetime | None = None
        self._last_promotion_at: datetime | None = None
        self._ranking_status: dict[str, object] = {
            "lastRankingAt": None,
            "rankingStatus": "NOT_RUN",
            "rankingSource": "NONE",
            "rankingScope": "UNAVAILABLE",
            "unavailableSignals": {},
            "weeklyGainerCount": 0,
            "turnoverLeaderCount": 0,
            "relativeVolumeLeaderCount": 0,
            "historicalBarRequestedCount": 0,
            "historicalBarSuccessCount": 0,
            "enrichedSymbolCount": 0,
            "mergedCandidateCount": 0,
            "filteredCandidateCount": 0,
            "acceptedCandidateCount": 0,
            "rejectionReasonCounts": {},
        }
        self._pipeline_counts: dict[str, int] = {
            "scanUniverseCount": 0,
            "researchCandidateCount": 0,
            "pendingResearchCount": 0,
            "qualifiedCandidateCount": 0,
            "promotedCandidateCount": 0,
            "tradeWatchlistCount": 0,
        }
        self._last_portfolio_scan: datetime | None = None
        self._last_tick_at: datetime | None = None
        self._last_evaluated_symbols: list[str] = []
        self._last_order_timeout_check: datetime | None = None

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
            "lastTickAt": self._last_tick_at.isoformat()
            if self._last_tick_at
            else None,
            "lastEvaluatedSymbols": list(self._last_evaluated_symbols),
            "lastDiscoveryRunAt": (
                self._last_discovery_run.isoformat()
                if self._last_discovery_run
                else None
            ),
            "lastResearchRunAt": (
                self._last_research_run.isoformat() if self._last_research_run else None
            ),
            "lastPromotionAt": (
                self._last_promotion_at.isoformat() if self._last_promotion_at else None
            ),
            **self._pipeline_counts,
            **self._ranking_status,
            "lastPortfolioScanAt": (
                self._last_portfolio_scan.isoformat()
                if self._last_portfolio_scan
                else None
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
        """Run one scanner tick and refresh research-pipeline status on every exit."""
        try:
            return await self._tick()
        finally:
            # Status is operational visibility, not a trading capability. Keep it
            # current even when a kill switch, gateway failure, cutoff, or account
            # readiness gate stops the trade-facing part of this tick.
            await self._refresh_pipeline_status()

    async def _tick(self) -> list[str]:
        """Run one scanner cycle and return evaluated trade symbols for tests."""
        self._last_tick_at = datetime.now(timezone.utc)
        self._last_evaluated_symbols = []
        # ── Runtime config (kill switch, cutoff, semboller, interval) ──────
        kill_switch = False
        runtime_cfg = risk_config
        scan_interval_minutes = 30
        scanner_runtime_enabled = True
        allow_orders = settings.scanner_allow_orders
        pending_overrides: set[str] = set()
        try:
            async with async_session_factory() as session:
                kill_switch = await is_kill_switch_enabled(session)
                runtime_cfg = await build_runtime_risk_config(session)
                profile = await get_active_profile(session)
                scan_interval_minutes = int(profile.scan_interval_minutes)
                scanner_runtime_enabled = await is_scanner_runtime_enabled(session)
                allow_orders = await get_scanner_allow_orders(session)
                pending_overrides = {
                    s.strip().upper()
                    for s in await list_pending_override_symbols(session)
                }
        except Exception:
            self._warn_throttled(
                "config", "Runtime config unavailable; using static .env defaults"
            )

        if kill_switch:
            self._warn_throttled(
                "killswitch", "Kill switch enabled; skipping scan cycle"
            )
            await notify_risk_block("Kill switch açık; scanner turu atlandı")
            return []

        if not scanner_runtime_enabled:
            # Admin panelden duraklatıldı (scannerEnabled=false). Stop-loss
            # bekçisi dahil hiçbir otomasyon bu turda çalışmaz.
            self._warn_throttled(
                "scanner-paused",
                "Scanner paused from admin panel (scannerEnabled=false)",
            )
            return []

        # ── Gateway sağlık kontrolü - Matriks kapalıysa tur atlanır ────────
        try:
            gateway_health = await self._gateway.health()
        except (GatewayUnavailable, GatewayError):
            self._warn_throttled(
                "gateway", "Matriks gateway unavailable; skipping scan cycle"
            )
            await notify_gateway_event("ulaşılamıyor")
            return []

        # v2 hesap izleyici (Faz 4): her tick'te kontrat sürümü + hesap
        # kimliği/türü/oturumu izlenir; değişimde account_events yazılır ve
        # REAL arming otomatik düşürülür. Sonuç burada sadece kayıt içindir —
        # emir yolu kendi taze health'iyle ayrıca kontrol eder.
        try:
            async with async_session_factory() as session:
                await account_watcher.check(gateway_health, session)
                await session.commit()
        except Exception:
            logger.exception("Account watcher tick check failed")

        # aiToolCallingEnabled panel anahtarı provider'a her tick yansıtılır
        # (restart'sız aç/kapat). Sadece DeepSeek'te anlamlıdır.
        try:
            async with async_session_factory() as session:
                tools_enabled = await get_ai_tool_calling_enabled(session)
            provider = get_default_provider()
            if getattr(provider, "tools_enabled", None) not in (None, tools_enabled):
                logger.info("AI tool-calling flag changed -> %s", tools_enabled)
            if hasattr(provider, "tools_enabled"):
                provider.tools_enabled = tools_enabled
        except Exception:
            logger.exception("AI tool-calling flag sync failed")

        # Discovery yalnızca gateway market-data yüzeyine dayanır; account
        # positionsLoaded hazır olana kadar bekletilirse research evreni boş
        # kalır. Discovery kendi movers/snapshot tazelik kontrollerini yapar
        # ve gateway tur ortasında düşerse hatayı yutup bu turu atlar.
        await self._run_discovery()
        # Research discovery is forced PAPER market-data work, so it must run
        # before account/trading gates and can never enter trade/order paths.
        await self._run_research(runtime_cfg._declined_set())
        # Bounded, market-data-only observation collection for outcome
        # measurement. Runs here (before the trading/cutoff gates) so a symbol
        # awaiting forward-return measurement is still observed after the
        # order cutoff, and so it can never sit on the order path. Fully
        # rate-limited and swallowed on failure (Fix 3).
        await self._run_observation_collector()
        # Trading duration intentionally excludes discovery and research work.
        trading_started = monotonic()

        # Aşağıdaki kapılar trade değerlendirmesi/portföy akışına aittir;
        # discovery'nin hazır olma koşullarının parçası değildir.
        if not runtime_cfg.can_trade_now():
            self._warn_throttled(
                "cutoff",
                f"Trading cutoff passed ({runtime_cfg.disable_trading_after} "
                f"{runtime_cfg.timezone}); skipping trade evaluation",
            )
            return []
        if not gateway_health.get("positionsLoaded"):
            logger.info("TRADING_SKIPPED_POSITIONS_NOT_LOADED")
            self._warn_throttled(
                "positions",
                "Matriks positions are not loaded; skipping trade and portfolio scans",
            )
            await notify_gateway_event("pozisyonlar yüklenmedi")
            # Discovery has already run above and only consumes market data.
            # Research also evaluates in PAPER mode, so keep it available
            # while the account position snapshot is warming up; do not enter
            # normal trade evaluation or the portfolio/order path.
            return []

        # Stale-order cancellation is an order-path operation and must honor
        # the same cutoff and positionsLoaded gates as normal trading.
        await self._run_order_timeout_check()
        # Deterministic stop-loss check - independent of AI, runs every tick
        # (not gated by scan_interval_minutes) so a stuck/HOLD AI cannot let
        # a losing position ride between evaluations.
        await self._run_stop_loss_guard()
        # ── Pozisyonları gateway'den tazele ────────────────────────────────
        # Admin panelinin Positions sayfası ve acil "tümünü sat" akışı
        # bot_positions'tan okuyor; eski push endpoint'i kaldırıldığı için
        # bu tabloyu güncel tutmak scanner'ın sorumluluğunda.
        # Position cache refresh runs in PositionSynchronizer so cutoff and
        # kill-switch scanner exits cannot leave the admin panel stale.

        # ── Sırası gelen sembolleri değerlendir ────────────────────────────
        # Normal scanner evaluates only promoted trade-watchlist symbols.
        # Research candidates have their own forced-PAPER pipeline below.
        symbols = await list_trade_eligible_symbols()
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
                # scannerAllowOrders=false (panel > env) -> PAPER'a sabit
                # (Phase 1 davranışı), emir yolu tamamen kapalı.
                result = await evaluate_symbol(
                    symbol,
                    mode=_configured_default_mode(),
                    force_paper=not allow_orders,
                )
            except GatewayUnavailable:
                self._warn_throttled(
                    "gateway",
                    "Gateway became unavailable mid-cycle; stopping this tick",
                )
                await notify_gateway_event("tur sırasında ulaşılamıyor")
                gateway_down_mid_cycle = True
                break
            except GatewayError as exc:
                logger.warning(
                    "Snapshot rejected by gateway symbol=%s error=%s", symbol, exc
                )
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
            await record_trade_watchlist_decision(result)

        # ── Otonom keşif (movers -> watchlist) + portföy re-evaluasyonu ────
        # Discovery yukarıda trade readiness kapılarından bağımsız çalıştı.
        # Gateway tur ortasında düştüyse research/portföy tekrar denenmez.
        if not gateway_down_mid_cycle:
            await self._run_portfolio_scan(pending_overrides)

        self._last_evaluated_symbols = list(evaluated)
        logger.info(
            "TRADING_SCAN_COMPLETED evaluatedCount=%s elapsedMs=%s status=%s",
            len(evaluated),
            int((monotonic() - trading_started) * 1000),
            "GATEWAY_UNAVAILABLE_MID_CYCLE" if gateway_down_mid_cycle else "COMPLETED",
        )
        return evaluated

    async def _run_order_timeout_check(self) -> None:
        now = datetime.now(timezone.utc)
        if self._last_order_timeout_check and (
            now - self._last_order_timeout_check
        ) < timedelta(minutes=1):
            return
        self._last_order_timeout_check = now
        await cancel_timed_out_orders(self._gateway, now=now)

    async def _run_stop_loss_guard(self) -> None:
        """Deterministic stop-loss check, independent of AI evaluation.

        check_stop_loss_positions only detects breaches; every triggered
        exit still goes through _maybe_send_order, so kill switch, cutoff,
        preflight, and cooldown gates apply exactly as for any other order.
        """
        try:
            triggered = await check_stop_loss_positions(self._gateway)
        except Exception:
            logger.exception("STOP_LOSS_GUARD_CHECK_FAILED")
            return
        for result in triggered:
            await self._maybe_send_order(result)
            stop_loss_guard.mark_triggered(result.response.symbol)

    async def _run_observation_collector(self) -> None:
        """Bounded market-data-only observation collection (Fix 3). Never
        raises and never enters the order path - it only records
        MarketObservation rows the outcome labeler later reads."""
        try:
            await market_observation_collector.run(self._gateway)
        except Exception:
            logger.exception("OBSERVATION_COLLECTOR_RUN_FAILED")

    async def _run_discovery(self) -> None:
        """Run low-cost movers screening and create research-only candidates."""
        policy = await load_research_policy()
        interval = timedelta(minutes=policy.discovery_interval_minutes)
        now = datetime.now(timezone.utc)
        if self._last_discovery_run and (now - self._last_discovery_run) < interval:
            return
        self._last_discovery_run = now

        started = monotonic()
        try:
            outcome = await run_discovery_scan(self._gateway)
        except Exception:
            logger.exception("Discovery scan failed")
            return
        elapsed_ms = int((monotonic() - started) * 1000)
        status = getattr(outcome, "status", "COMPLETED")
        self._ranking_status = {
            "lastRankingAt": self._last_discovery_run.isoformat(),
            "rankingStatus": status,
            "rankingSource": getattr(outcome, "ranking_source", "NONE"),
            "rankingScope": getattr(outcome, "ranking_scope", "UNAVAILABLE"),
            "unavailableSignals": dict(getattr(outcome, "unavailable_signals", {})),
            "weeklyGainerCount": getattr(outcome, "weekly_gainer_count", 0),
            "turnoverLeaderCount": getattr(outcome, "turnover_leader_count", 0),
            "relativeVolumeLeaderCount": getattr(
                outcome, "relative_volume_leader_count", 0
            ),
            "historicalBarRequestedCount": getattr(
                outcome, "historical_bar_requested_count", 0
            ),
            "historicalBarSuccessCount": getattr(
                outcome, "historical_bar_success_count", 0
            ),
            "enrichedSymbolCount": getattr(outcome, "enrichment_count", 0),
            "mergedCandidateCount": getattr(outcome, "candidate_count", 0),
            "filteredCandidateCount": getattr(outcome, "filtered_count", 0),
            "acceptedCandidateCount": len(outcome),
            "rejectionReasonCounts": dict(
                getattr(outcome, "rejection_reason_counts", {})
            ),
        }
        if status == "GATEWAY_UNAVAILABLE":
            logger.info(
                "DISCOVERY_SKIPPED_GATEWAY_UNAVAILABLE elapsedMs=%s", elapsed_ms
            )
            return
        if status == "MARKET_DATA_UNAVAILABLE":
            logger.info(
                "DISCOVERY_SKIPPED_MARKET_DATA_UNAVAILABLE elapsedMs=%s", elapsed_ms
            )
            return
        logger.info(
            "DISCOVERY_COMPLETED acceptedCount=%s candidateCount=%s universeCount=%s elapsedMs=%s",
            len(outcome),
            getattr(outcome, "candidate_count", 0),
            getattr(outcome, "universe_count", 0),
            elapsed_ms,
        )

    async def _run_research(self, declined_symbols: set[str]) -> None:
        """Evaluate due candidates in forced PAPER mode, then maintain eligibility."""
        started = monotonic()
        try:
            self._last_research_run = datetime.now(timezone.utc)
            evaluated = await run_research_cycle(self._gateway)
            removed = await maintain_trade_watchlist(declined_symbols)
            logger.info(
                "RESEARCH_COMPLETED evaluatedCount=%s watchlistRemovedCount=%s elapsedMs=%s",
                len(evaluated),
                len(removed),
                int((monotonic() - started) * 1000),
            )
        except Exception:
            logger.exception("Research pipeline cycle failed")

    async def _refresh_pipeline_status(self) -> None:
        try:
            counts = await get_pipeline_counts()
            async with async_session_factory() as session:
                universe = await get_admin_config_value(session, "scanUniverseSymbols")
                latest_promotion = (
                    await session.execute(
                        select(TradeWatchlistSymbol.eligible_at)
                        .where(TradeWatchlistSymbol.source == "RESEARCH_PROMOTION")
                        .order_by(TradeWatchlistSymbol.eligible_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
            counts["scanUniverseCount"] = len(
                {item.strip().upper() for item in universe.split(",") if item.strip()}
            )
            self._pipeline_counts = counts
            if latest_promotion is not None:
                if latest_promotion.tzinfo is None:
                    latest_promotion = latest_promotion.replace(tzinfo=timezone.utc)
                self._last_promotion_at = latest_promotion
        except Exception:
            logger.exception("Research pipeline status refresh failed")

    async def _run_portfolio_scan(self, pending_overrides: set[str]) -> None:
        """Eldeki pozisyonları periyodik yeniden değerlendir (Portfolio Manager).

        ``bot_positions``ta lot bulunan her sembol LLM'e pozisyon bağlamıyla
        gider (evaluator ``positionContext`` ekler; prompt kural 16: kar al /
        zarar kes / tut). Normal tarama zaten pozisyonlu sembolleri kapsıyor
        olabilir - bu döngü, izleme listesinden çıkmış (ör. watchlist'ten
        alınmış sonra pasifleşmiş) pozisyonların da yönetimsiz kalmamasını
        garanti eder.
        """
        try:
            async with async_session_factory() as session:
                interval_minutes = await get_portfolio_scan_interval_minutes(session)
                allow_orders = await get_scanner_allow_orders(session)
        except Exception:
            interval_minutes = settings.portfolio_scan_interval_minutes
            allow_orders = settings.scanner_allow_orders
        interval = timedelta(minutes=max(5, interval_minutes))
        now = datetime.now(timezone.utc)
        if self._last_portfolio_scan and (now - self._last_portfolio_scan) < interval:
            return
        self._last_portfolio_scan = now

        try:
            async with async_session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            select(BotPosition).where(BotPosition.qty > 0)
                        )
                    )
                    .scalars()
                    .all()
                )
        except Exception:
            logger.exception("Portfolio scan: bot_positions read failed")
            return

        held = {
            row.symbol.strip().upper(): float(row.qty or 0) for row in rows
        }
        if not held:
            return

        logger.info("PORTFOLIO_SCAN_STARTED positionCount=%s", len(held))
        for symbol, held_qty in held.items():
            if self._stop_event.is_set():
                break
            # v2 önem kapısı (Faz 5): AI yalnızca son AI çağrısından bu yana
            # anlamlı değişiklik varsa çağrılır. Gözlem kurulamazsa fail-open
            # → normal değerlendirme yoluna devam (AI kendi veri kalitesi
            # kontrollerini yapar). Stop-loss bekçisi bu kapıdan bağımsızdır.
            observation = None
            try:
                observation = await self._build_portfolio_observation(
                    symbol, held_qty
                )
            except GatewayUnavailable:
                self._warn_throttled(
                    "gateway", "Gateway unavailable during portfolio scan; stopping"
                )
                break
            except Exception:
                logger.exception(
                    "Portfolio observation failed symbol=%s — evaluating anyway",
                    symbol,
                )
            if observation is not None:
                threshold = await self._significance_threshold()
                verdict = significance_detector.assess(
                    observation, price_move_pct=threshold
                )
                if not verdict.significant:
                    logger.info("PORTFOLIO_SCAN_SKIP symbol=%s triggers=[]", symbol)
                    continue
                logger.info(
                    "PORTFOLIO_SCAN_SIGNIFICANT symbol=%s triggers=%s",
                    symbol,
                    ",".join(verdict.triggers),
                )
            try:
                result = await evaluate_symbol(
                    symbol,
                    mode=_configured_default_mode(),
                    force_paper=not allow_orders,
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

            # Normal tarama zamanlayıcısını da tazele - aynı tick içinde
            # sembol ikinci kez değerlendirilmesin.
            self._last_scan_by_symbol[symbol] = now
            # Baseline YALNIZCA gerçek bir LLM kararından sonra güncellenir
            # (Fix #6): preflight-gate WAIT'i, admin override veya sistem
            # kapısı baseline oluşturmamalı — yoksa değişimler kaçırılır.
            if observation is not None and result.decision_source == "llm":
                significance_detector.record_ai_evaluation(observation)
            response = result.response
            logger.info(
                "Portfolio decision symbol=%s action=%s confidence=%s allowOrder=%s",
                symbol,
                response.action.value,
                response.confidence_score,
                response.allow_order,
            )
            await self._maybe_send_order(result)

    async def _significance_threshold(self) -> Decimal:
        try:
            async with async_session_factory() as session:
                raw = await get_admin_config_value(
                    session, "significancePriceMovePct"
                )
            return Decimal(str(raw))
        except Exception:
            return Decimal("1.5")

    async def _build_portfolio_observation(
        self, symbol: str, held_qty: float
    ):
        """Önem değerlendirmesi için ucuz, LLM'siz gözlem üret."""
        snapshot = await self._gateway.get_snapshot(symbol)
        payload = snapshot.get("payload") or {}
        async with async_session_factory() as session:
            news_fp, kap_fp = await load_event_fingerprints(session, symbol)
            lifecycle = (
                await session.execute(
                    select(PositionLifecycle)
                    .where(
                        PositionLifecycle.symbol == symbol,
                        PositionLifecycle.status == "OPEN",
                    )
                    .limit(1)
                )
            ).scalars().first()
        active_stop = (
            float(lifecycle.active_stop_loss)
            if lifecycle is not None and lifecycle.active_stop_loss is not None
            else None
        )
        return build_observation(
            symbol,
            payload,
            position_qty=held_qty,
            active_stop=active_stop,
            news_fp=news_fp,
            kap_fp=kap_fp,
        )

    # ── Order path (Phase 2) ───────────────────────────────────────────────

    async def _maybe_send_order(self, result: EvaluationResult) -> None:
        """Karar emre dönüşmeli mi? Tüm kapılar geçerse gateway'e gönder.

        Buradaki kapılar ilk savunma hattı; gateway (C#) aynı kontrolleri
        kendi sabit limitleriyle bir kez daha uygular.
        """
        response = result.response
        # Fix #2: startup disarm başarısızsa süreç boyunca sert blok
        # (DB'den bağımsız fail-closed) — hiçbir emir gönderilmez.
        if is_dispatch_blocked():
            logger.warning(
                "Order blocked: dispatch hard-blocked (%s) requestId=%s",
                dispatch_block_reason(),
                response.request_id,
            )
            return
        if response.requires_confirmation:
            await queue_response(response, result.mode)
            return
        if not await _orders_enabled() or not response.allow_order:
            return
        # v2 çift kapı (Faz 4): systemMode=AUTO_TRADE değilse hiçbir emir
        # gönderilmez. Eski mod kapıları aşağıda AYNEN durur — geçiş dönemi
        # boyunca dispatch için ikisi birden açık olmalı (fail-closed AND).
        try:
            async with async_session_factory() as session:
                system_mode = await get_system_mode(session)
        except Exception:
            logger.exception("systemMode read failed — blocking dispatch")
            return
        if system_mode != "AUTO_TRADE":
            logger.info(
                "Order blocked: systemMode=%s (OBSERVE_ONLY) requestId=%s",
                system_mode,
                response.request_id,
            )
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
        parsed_qty = parse_finite_decimal(response.qty)
        parsed_price = parse_finite_decimal(response.price)
        if (
            isinstance(response.qty, bool)
            or not isinstance(response.qty, int)
            or parsed_qty is None
            or parsed_qty <= 0
            or parsed_qty != parsed_qty.to_integral_value()
            or parsed_price is None
            or parsed_price <= 0
        ):
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

        if response.action == SignalAction.BUY and not await is_trade_eligible(
            response.symbol
        ):
            logger.warning(
                "BUY blocked: symbol is not trade eligible symbol=%s requestId=%s",
                response.symbol,
                response.request_id,
            )
            return
        if (
            response.action == SignalAction.BUY
            and stop_loss_guard.is_symbol_cooling_down(response.symbol)
        ):
            logger.warning(
                "BUY blocked: stop-loss guard cooldown active symbol=%s requestId=%s",
                response.symbol,
                response.request_id,
            )
            return

        dispatch_account_ref: str | None = None
        try:
            async with async_session_factory() as session:
                if await is_kill_switch_enabled(session):
                    logger.warning(
                        "Order dispatch blocked by kill switch requestId=%s",
                        response.request_id,
                    )
                    return
                preflight_config = await build_runtime_risk_config(session)
            if not preflight_config.can_trade_now():
                logger.warning(
                    "Order dispatch blocked by trading cutoff requestId=%s",
                    response.request_id,
                )
                return
            fresh_snapshot = await self._gateway.get_snapshot(response.symbol)
            fresh_positions = await self._gateway.get_positions()
            fresh_health = await self._gateway.health()
            # v2 emir öncesi hesap yeniden doğrulaması (ilke #5): kontrat
            # sürümü + hesap kimliği/türü/oturumu taze health'ten kontrol
            # edilir; değişim tespiti otomatik disarm + blok üretir.
            async with async_session_factory() as session:
                account_check = await account_watcher.check(fresh_health, session)
                await session.commit()
            if not account_check.dispatch_allowed:
                logger.warning(
                    "Order blocked by account watcher requestId=%s reason=%s",
                    response.request_id,
                    account_check.reason,
                )
                return
            # Bu emrin sabit hesap referansı (fill damgalama için, Fix #1).
            dispatch_account_ref = account_check.account_ref
            preflight_reason = validate_order_preflight(
                payload=fresh_snapshot.get("payload") or {},
                positions=fresh_positions,
                health=fresh_health,
                side=response.action.value,
                qty=response.qty,
                limit_price=response.price,
                decision_created_utc=result.decision_created_utc,
                max_spread_pct=preflight_config.max_spread_pct_for_buy,
            )
            account_inputs = None
            if response.action == SignalAction.BUY:
                account_inputs = await fetch_fresh_account_inputs(
                    self._gateway,
                    symbol=response.symbol,
                    target_snapshot=fresh_snapshot,
                )
        except Exception as exc:
            preflight_reason = "order-time snapshot unavailable: " + str(exc)
        if preflight_reason:
            logger.warning(
                "Order preflight blocked requestId=%s reason=%s",
                response.request_id,
                preflight_reason,
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
                                (
                                    "SENT_PENDING",
                                    "NEW",
                                    "A",
                                    "PARTIALLY_FILLED",
                                    "FILLED",
                                )
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
        # v2 audit-yoksa-emir-yok kapısı (ilke #6): nihai karar audit kaydı
        # (normal yol: risk_decisions; stop-loss yolu: STOP_BREACHED olayı)
        # DB'de COMMIT edilmiş olmadan gateway'e POST atılmaz. DB okunamazsa
        # da emir gönderilmez (fail-closed).
        try:
            if not await _decision_audit_exists(response.request_id):
                logger.warning(
                    "Order blocked: no committed decision audit record "
                    "requestId=%s",
                    response.request_id,
                )
                return
        except Exception:
            logger.exception(
                "Decision audit check failed — blocking dispatch requestId=%s",
                response.request_id,
            )
            return

        if last_sent is not None and now - last_sent < _ORDER_COOLDOWN:
            remaining = _ORDER_COOLDOWN - (now - last_sent)
            logger.warning(
                "Order skipped: cooldown active symbol=%s side=%s remaining=%ss requestId=%s",
                response.symbol,
                response.action.value,
                max(1, int(remaining.total_seconds())),
                response.request_id,
            )
            await notify_order_event(
                "COOLDOWN",
                symbol=response.symbol,
                side=response.action.value,
                qty=response.qty,
                price=response.price,
                request_id=response.request_id,
                reason="Emir cooldown süresinde",
            )
            return

        if response.action == SignalAction.BUY:
            if account_inputs is None:
                logger.warning(
                    "BUY blocked: fresh account inputs missing requestId=%s",
                    response.request_id,
                )
                return
            if response.stop_loss is None or response.target_price is None:
                logger.warning(
                    "BUY blocked: entry/stop/target incomplete requestId=%s",
                    response.request_id,
                )
                return
            try:
                async with async_session_factory() as session:
                    limits = await resolve_effective_risk_config(session)
                    reservation_handling = await get_account_reservation_handling(
                        session
                    )
                    adapter = MatriksAccountContextAdapter(
                        reservation_handling=reservation_handling,
                        allow_margin_buying=limits.allow_margin_buying,
                        max_account_data_age_seconds=(
                            limits.max_account_data_age_seconds
                        ),
                    )
                    reservation = await reserve_sized_buy(
                        session,
                        request_id=response.request_id,
                        symbol=response.symbol,
                        original_decision_qty=response.qty,
                        limit_price=Decimal(str(response.price)),
                        mode=result.mode.value,
                        raw_account=account_inputs.raw_account,
                        raw_positions=account_inputs.raw_positions,
                        raw_open_orders=account_inputs.raw_open_orders,
                        market_prices=account_inputs.market_prices,
                        trade=TradeSizingContext(
                            symbol=response.symbol,
                            entry_price=response.price,
                            stop_loss=response.stop_loss,
                            target_price=response.target_price,
                            confidence=Decimal(str(response.confidence_score)),
                            current_price=account_inputs.market_prices[
                                response.symbol.strip().upper()
                            ],
                        ),
                        limits=limits,
                        adapter=adapter,
                        account_ref=dispatch_account_ref,
                    )
                if not reservation.allowed:
                    logger.warning(
                        "BUY reservation blocked requestId=%s reason=%s",
                        response.request_id,
                        reservation.reason,
                    )
                    return
                response.qty = reservation.qty
                ledger_row = reservation.ledger
                if ledger_row is None:
                    return
            except Exception as exc:
                logger.exception(
                    "BUY account sizing/reservation failed requestId=%s",
                    response.request_id,
                )
                await notify_risk_block(
                    f"Fresh account sizing unavailable: {exc}",
                    {"symbol": response.symbol, "requestId": response.request_id},
                )
                return
        else:
            async with async_session_factory() as session:
                ledger_row, may_send, ledger_rejection = await reserve_order(
                    session,
                    request_id=response.request_id,
                    symbol=response.symbol,
                    side=response.action.value,
                    qty=response.qty,
                    limit_price=response.price,
                    mode=result.mode.value,
                    account_ref=dispatch_account_ref,
                )
                if not may_send:
                    logger.warning(
                        "Order replay blocked requestId=%s reason=%s",
                        response.request_id,
                        ledger_rejection or ledger_row.status,
                    )
                    return
                await mark_send_started(session, ledger_row)

        # Fix #1 (fail-closed): hesap referansı emir GÖNDERİLMEDEN önce
        # OrderLog'a KESİN olarak yazılmış olmalı. Rezervasyon bunu atomik
        # yazdı; burada doğrulanıyor. dispatch_account_ref boşsa, OrderLog
        # bulunamazsa, account_ref beklenenle uyuşmuyorsa VEYA doğrulama
        # DB hatası verirse: emir GÖNDERİLMEZ, ledger REJECTED'a çekilir
        # (rezervasyon serbest bırakılır) ve tur atlanır. Aksi halde fill'ler
        # hangi hesaba yazılacağı belirsiz kalırdı (DEMO/REAL karışması riski).
        async def _block_order(block_reason: str) -> None:
            logger.error(
                "Order blocked (account_ref fail-closed) requestId=%s reason=%s",
                response.request_id,
                block_reason,
            )
            try:
                async with async_session_factory() as session:
                    olog = (
                        await session.execute(
                            select(OrderLog).where(
                                OrderLog.request_id == response.request_id
                            )
                        )
                    ).scalar_one_or_none()
                    if olog is not None:
                        await mark_send_result(
                            session,
                            olog,
                            status="REJECTED",
                            message=f"account_ref stamping failed: {block_reason}",
                        )
            except Exception:
                logger.exception(
                    "Failed to mark ledger REJECTED after account_ref block "
                    "requestId=%s",
                    response.request_id,
                )
            await notify_risk_block(
                f"Order blocked (account_ref): {block_reason}",
                {"symbol": response.symbol, "requestId": response.request_id},
            )

        if not dispatch_account_ref:
            await _block_order("dispatch account_ref is empty")
            return
        try:
            async with async_session_factory() as session:
                olog = (
                    await session.execute(
                        select(OrderLog).where(
                            OrderLog.request_id == response.request_id
                        )
                    )
                ).scalar_one_or_none()
            if olog is None:
                await _block_order("OrderLog not found before send")
                return
            if olog.account_ref != dispatch_account_ref:
                await _block_order(
                    f"OrderLog.account_ref mismatch "
                    f"(stored={olog.account_ref!r} expected={dispatch_account_ref!r})"
                )
                return
        except Exception as exc:
            await _block_order(f"account_ref verification error: {exc}")
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
            await notify_order_event(
                status,
                symbol=response.symbol,
                side=response.action.value,
                qty=response.qty,
                price=response.price,
                request_id=response.request_id,
                reason=reason,
            )
        except (GatewayUnavailable, GatewayError) as exc:
            status = "SEND_UNKNOWN"
            reason = str(exc)
            logger.error(
                "Order send failed symbol=%s requestId=%s error=%s",
                response.symbol,
                response.request_id,
                exc,
            )
            await notify_order_event(
                status,
                symbol=response.symbol,
                side=response.action.value,
                qty=response.qty,
                price=response.price,
                request_id=response.request_id,
                reason=reason,
            )

        async with async_session_factory() as session:
            row = (
                await session.execute(
                    select(OrderLog).where(OrderLog.request_id == response.request_id)
                )
            ).scalar_one()
            await mark_send_result(
                session,
                row,
                status=status,
                message=f"scanner: {reason}",
                uncertain=status == "SEND_UNKNOWN",
            )
        # Compatibility hook for notifications/tests; the ledger has already
        # committed the authoritative state above.
        await self._persist_order_outcome(response, status, reason)

    async def _persist_order_outcome(self, response, status: str, reason: str) -> None:
        """Senkron emir sonucunu order_logs'a yaz.

        Gateway'in OnOrderUpdate raporu /api/order-result üzerinden ayrıca
        gelir; bu kayıt gönderim anındaki sonucu (SENT_PENDING/REJECTED/ERROR)
        tutar ki reddedilen emirler de izlenebilsin.
        """
        try:
            async with async_session_factory() as session:
                entry = (
                    await session.execute(
                        select(OrderLog).where(
                            OrderLog.request_id == response.request_id
                        )
                    )
                ).scalar_one_or_none()
                if entry is None:
                    entry = OrderLog(
                        request_id=response.request_id,
                        symbol=response.symbol,
                        action=response.action.value,
                        qty=response.qty,
                        price=response.price or 0.0,
                        status=status,
                        order_id=None,
                        matrix_message=f"scanner: {reason}",
                    )
                    session.add(entry)
                else:
                    entry.status = status
                    entry.matrix_message = f"scanner: {reason}"
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
