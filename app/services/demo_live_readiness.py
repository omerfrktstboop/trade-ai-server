"""Read-only DEMO_LIVE readiness checker (Task 9-10). Never sends an order,
never modifies configuration, never touches scanner/admin-config settings -
strictly observational.

Two kinds of checks, deliberately kept separate:
- Config-declared state (admin config / active trade profile / DB), read
  directly from the database - true regardless of which process reads it.
- Confirmed-live state (is the scanner loop actually running right now),
  which only the live FastAPI process's own memory can answer. A fresh CLI
  process has no way to observe another process's in-memory scanner
  singleton, so this is checked via one HTTP call to that process's own
  /admin/api/bot-status endpoint; if it's unreachable, the check is reported
  as UNKNOWN rather than guessed.

Task 10 also reports a static, code-verified finding rather than a live
probe: TradeAiGateway.cs's ReadDepthSnapshot() unconditionally sets
DepthReliable=false / DepthAgeSeconds=double.MaxValue after computing the
real values (Matriks does not expose a verified per-event depth timestamp),
so ValidateOrderMarketData() rejects every single order with "depth is
unavailable or stale" regardless of admin config. This module does not call
the gateway to "detect" this - it would be indistinguishable from a real
transient depth outage - it reports the source-verified constraint directly
without altering the underlying (intentionally fail-closed) depth check.

Callable as:
    python -m app.services.demo_live_readiness
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

from app.config import settings
from app.db.session import async_session_factory
from app.services.admin_config import get_admin_config_value
from app.services.ai_provider import get_ai_provider_status
from app.services.matriks_gateway import GatewayError, GatewayUnavailable, gateway_client
from app.services.research_pipeline import get_pipeline_counts, list_trade_eligible_symbols
from app.services.strategy_provenance import resolve_ai_provider_model
from app.services.trade_profile import get_active_profile

logger = logging.getLogger(__name__)

# Source-level status of the depth-freshness blocker in
# matriks/TradeAiGateway.cs. ReadDepthSnapshot() used to unconditionally
# overwrite DepthReliable=false / DepthAgeSeconds=double.MaxValue after
# AnalyzeDepth() computed the real structural values, because Matriks
# exposes no depth-specific event timestamp anywhere in this SDK surface
# (confirmed: no OnDepthUpdate/OnMarketDepthChanged callback, no timestamp
# field on the row objects). That has been fixed in source: ReadDepthSnapshot
# now derives depth age from _lastTradeUtcBySymbol - a genuine, event-driven
# timestamp (populated only by the real OnDataUpdate push event) for the
# same live Matriks subscription depth is delivered on - instead of always
# failing. DepthReliable still comes from AnalyzeDepth's real bid/ask
# structural checks, untouched.
#
# This is a *source* fix only. matriks/TradeAiGateway.cs is compiled/loaded
# by Matriks IQ itself, not by this Python server - a `/config/reload` call
# only refreshes config values, it does not recompile the algo's code. The
# fix only takes effect once the algo is reloaded/restarted inside Matriks
# IQ, and this readiness check has no live signal to confirm that happened
# (the /health payload does not expose a code-version marker for the algo).
# Until an operator confirms the algo was reloaded after this fix, treat the
# blocker as still potentially live on the running process.
DEPTH_TIMESTAMP_SOURCE_FIX_APPLIED = True
DEPTH_BLOCKER_DETAIL = (
    "matriks/TradeAiGateway.cs: ReadDepthSnapshot() has been fixed to derive "
    "DepthAgeSeconds/DepthTimestamp from _lastTradeUtcBySymbol (the same "
    "genuine, event-driven quote-tick timestamp already required to be fresh "
    "earlier in ValidateOrderMarketData), instead of unconditionally failing. "
    "DepthReliable is still gated by AnalyzeDepth's real bid/ask structure, "
    "unchanged. THIS REQUIRES THE MATRIKS IQ ALGO TO BE RELOADED/RESTARTED "
    "before it takes effect - a gateway /config/reload call does not "
    "recompile the algo. This readiness check cannot verify live whether the "
    "running gateway process has picked up the fix yet; re-run this check "
    "after confirming the algo was reloaded in Matriks IQ."
)


@dataclass
class FastApiStatus:
    reachable: bool = False
    error: str | None = None
    scanner_process_started: str = "UNKNOWN"
    scanner_enabled: bool | None = None
    scanner_allow_orders: bool | None = None
    trading_mode: str | None = None
    kill_switch_enabled: bool | None = None
    trading_kill_switch_active: bool | None = None
    force_safe_mode: bool | None = None
    active_profile_code: str | None = None


@dataclass
class GatewayStatus:
    reachable: bool = False
    error: str | None = None
    config_loaded: bool | None = None
    config_stale: bool | None = None
    runtime_mode: str | None = None
    positions_loaded: bool | None = None
    enable_demo_orders: bool | None = None
    enable_real_orders: bool | None = None
    real_live_mode_allowed: bool | None = None
    real_live_armed: bool | None = None
    require_demo_account: bool | None = None
    demo_account_confirmed: bool | None = None
    test_auto_order_enabled: bool | None = None
    max_order_value_tl: float | None = None
    max_qty_per_order: float | None = None
    max_orders_per_day: int | None = None
    max_orders_per_symbol_per_day: int | None = None
    symbols_count: int = 0


@dataclass
class ResearchPipelineStatus:
    scan_universe_count: int = 0
    research_candidate_count: int = 0
    pending_research_count: int = 0
    qualified_candidate_count: int = 0
    trade_watchlist_count: int = 0
    buy_allowed_symbols: list[str] = field(default_factory=list)


@dataclass
class AiProviderStatus:
    provider: str | None = None
    model: str | None = None
    is_degraded: bool | None = None
    consecutive_failures: int | None = None


@dataclass
class ReadinessReport:
    fastapi: FastApiStatus = field(default_factory=FastApiStatus)
    gateway: GatewayStatus = field(default_factory=GatewayStatus)
    research: ResearchPipelineStatus = field(default_factory=ResearchPipelineStatus)
    ai: AiProviderStatus = field(default_factory=AiProviderStatus)
    # True until an operator confirms (outside this checker's reach) that
    # the Matriks IQ algo was reloaded after the ReadDepthSnapshot source
    # fix - see DEPTH_BLOCKER_DETAIL.
    depth_fix_pending_gateway_reload_confirmation: bool = DEPTH_TIMESTAMP_SOURCE_FIX_APPLIED
    depth_order_blocker_detail: str = DEPTH_BLOCKER_DETAIL
    notes: list[str] = field(default_factory=list)
    verdict: str = "DEMO_LIVE NO-GO"


async def _check_fastapi_live_process() -> tuple[bool, str | None, str]:
    """The only fact a separate CLI process cannot get from the DB: whether
    the scanner loop is actually running inside the live server process."""
    url = f"http://127.0.0.1:{settings.port}/api/admin/bot-status"
    headers = {"Authorization": f"Bearer {settings.effective_admin_api_token}"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, headers=headers)
        if response.status_code != 200:
            return False, f"HTTP {response.status_code} from {url}", "UNKNOWN"
        data = response.json()
        running = bool((data.get("scanner") or {}).get("running"))
        return True, None, "STARTED" if running else "NOT_RUNNING"
    except Exception as exc:
        return False, f"FASTAPI_SERVER_UNREACHABLE: {exc}", "UNKNOWN"


async def _build_fastapi_status(session) -> FastApiStatus:
    status = FastApiStatus()
    status.scanner_enabled = await get_admin_config_value(session, "scannerEnabled") == "true"
    status.scanner_allow_orders = (
        await get_admin_config_value(session, "scannerAllowOrders") == "true"
    )
    status.trading_mode = await get_admin_config_value(session, "tradingMode")
    status.kill_switch_enabled = (
        await get_admin_config_value(session, "killSwitchEnabled") == "true"
    )
    status.trading_kill_switch_active = (
        await get_admin_config_value(session, "tradingKillSwitchActive") == "true"
    )
    status.force_safe_mode = await get_admin_config_value(session, "forceSafeMode") == "true"
    active_profile = await get_active_profile(session)
    status.active_profile_code = active_profile.code

    reachable, error, running_state = await _check_fastapi_live_process()
    status.reachable = reachable
    status.error = error
    status.scanner_process_started = running_state
    return status


async def _build_gateway_status() -> GatewayStatus:
    status = GatewayStatus()
    try:
        health = await gateway_client.health()
    except (GatewayUnavailable, GatewayError) as exc:
        status.error = str(exc)
        return status
    except Exception as exc:
        logger.exception("READINESS_GATEWAY_HEALTH_FAILED")
        status.error = f"{type(exc).__name__}: {exc}"
        return status

    status.reachable = True
    status.config_loaded = health.get("configLoaded")
    status.config_stale = health.get("configStale")
    status.runtime_mode = health.get("runtimeMode")
    status.positions_loaded = health.get("positionsLoaded")
    status.test_auto_order_enabled = health.get("testAutoOrderEnabled")
    status.symbols_count = len(health.get("symbols") or [])

    limits = health.get("orderLimits") or {}
    status.enable_demo_orders = limits.get("enableDemoOrders")
    status.enable_real_orders = limits.get("enableRealOrders")
    status.real_live_mode_allowed = limits.get("realLiveModeAllowed")
    status.real_live_armed = limits.get("realLiveArmed")
    status.require_demo_account = limits.get("requireDemoAccount")
    status.demo_account_confirmed = limits.get("demoAccountConfirmed")
    status.max_order_value_tl = limits.get("maxOrderValueTl")
    status.max_qty_per_order = limits.get("maxQtyPerOrder")
    status.max_orders_per_day = limits.get("maxOrdersPerDay")
    status.max_orders_per_symbol_per_day = limits.get("maxOrdersPerSymbolPerDay")
    return status


async def _build_research_status() -> ResearchPipelineStatus:
    status = ResearchPipelineStatus()
    async with async_session_factory() as session:
        universe = await get_admin_config_value(session, "scanUniverseSymbols")
    status.scan_universe_count = len(
        {s.strip().upper() for s in universe.split(",") if s.strip()}
    )
    counts = await get_pipeline_counts()
    status.research_candidate_count = counts.get("researchCandidateCount", 0)
    status.pending_research_count = counts.get("pendingResearchCount", 0)
    status.qualified_candidate_count = counts.get("qualifiedCandidateCount", 0)
    status.trade_watchlist_count = counts.get("tradeWatchlistCount", 0)
    status.buy_allowed_symbols = sorted(await list_trade_eligible_symbols())
    return status


async def _build_ai_status() -> AiProviderStatus:
    status = AiProviderStatus()
    provider_status = get_ai_provider_status()
    status.is_degraded = provider_status.get("isDegraded")
    status.consecutive_failures = provider_status.get("consecutiveFailures")
    provider, model = resolve_ai_provider_model()
    status.provider = provider
    status.model = model
    return status


def _compute_verdict(report: ReadinessReport) -> str:
    if report.depth_fix_pending_gateway_reload_confirmation:
        # Conservative default: the source fix exists, but this checker has
        # no way to confirm the running Matriks IQ algo has actually
        # reloaded it - see DEPTH_BLOCKER_DETAIL for the exact manual step.
        return "DEMO_LIVE NO-GO"
    if not report.gateway.reachable or report.gateway.config_stale is True:
        return "DEMO_LIVE NO-GO"
    if report.gateway.positions_loaded is not True:
        return "DEMO_LIVE NO-GO"
    if report.gateway.demo_account_confirmed is not True:
        return "DEMO_LIVE NO-GO"
    if report.ai.provider == "mock":
        return "DEMO_LIVE NO-GO"
    if not (
        report.fastapi.scanner_enabled
        and report.fastapi.scanner_allow_orders
        and report.fastapi.trading_mode == "DEMO_LIVE"
        and report.fastapi.kill_switch_enabled is False
        and report.fastapi.trading_kill_switch_active is False
        and report.fastapi.force_safe_mode is False
    ):
        return "DEMO_LIVE NO-GO"
    if report.research.trade_watchlist_count == 0:
        return "DEMO_LIVE READY — WAITING FOR TRADE WATCHLIST"
    return "DEMO_LIVE READY"


async def run_readiness_check() -> ReadinessReport:
    report = ReadinessReport()
    async with async_session_factory() as session:
        report.fastapi = await _build_fastapi_status(session)
        # An operator-set confirmation, not an auto-detected fact: this
        # checker has no live signal for whether the Matriks IQ algo was
        # reloaded after the ReadDepthSnapshot source fix, so it trusts an
        # explicit admin-panel acknowledgement instead of guessing.
        confirmed = await get_admin_config_value(session, "depthTimestampFixConfirmed")
    report.depth_fix_pending_gateway_reload_confirmation = confirmed != "true"
    report.gateway = await _build_gateway_status()
    report.research = await _build_research_status()
    report.ai = await _build_ai_status()

    if not report.fastapi.reachable:
        report.notes.append(
            "FASTAPI_SERVER_UNREACHABLE: scanner_process_started could not be "
            "confirmed live; config-declared values above still reflect the "
            f"database. ({report.fastapi.error})"
        )
    if report.ai.provider == "mock":
        report.notes.append(
            "AI_PROVIDER_MOCK_NO_REAL_STRATEGY_DECISIONS: mock provider always "
            "returns WAIT - no real BUY/SELL decisions will ever be produced."
        )
    if report.research.trade_watchlist_count == 0:
        report.notes.append(
            "WAITING_FOR_RESEARCH_PROMOTION: Trade Watchlist is empty - this is "
            "not a config error, it means research has not yet produced the "
            "required consecutive passes to promote a symbol."
        )
    if report.depth_fix_pending_gateway_reload_confirmation:
        report.notes.append(
            "DEPTH_TIMESTAMP_FIX_PENDING_GATEWAY_RELOAD: " + report.depth_order_blocker_detail
        )

    report.verdict = _compute_verdict(report)
    return report


def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    report = asyncio.run(run_readiness_check())
    logger.info("DEMO_LIVE_READINESS fastapi=%s", report.fastapi)
    logger.info("DEMO_LIVE_READINESS gateway=%s", report.gateway)
    logger.info("DEMO_LIVE_READINESS research=%s", report.research)
    logger.info("DEMO_LIVE_READINESS ai=%s", report.ai)
    for note in report.notes:
        logger.warning(note)
    logger.info("DEMO_LIVE_READINESS_VERDICT %s", report.verdict)


if __name__ == "__main__":
    _main()
