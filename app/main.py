"""FastAPI application entry point."""

import sys
from contextlib import AsyncExitStack, asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Windows'ta stdout dosyaya/servise yönlendirildiğinde varsayılan cp1254
# encoding'i emoji içeren startup print'lerinde UnicodeEncodeError ile
# uygulamayı çökertir (hedef deploy: Windows Server + NSSM).
for _stream in (sys.stdout, sys.stderr):
    if _stream.encoding and _stream.encoding.lower() not in ("utf-8", "utf8"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.config import settings
from app.core.logger import configure_file_logging

configure_file_logging()

from app.db.init_db import init_db
from app.routers.admin import admin_api_router, admin_router
from app.routers.gateway_log import router as gateway_log_router
from app.routers.health import router as health_router
from app.routers.gateway_config import router as gateway_config_router
from app.routers.order_result import router as order_result_router
from app.routers.signal import router as signal_router
from app.routers.signal_history import router as signal_history_router

# MCP read-only tool sunucusu (v2). mcp paketi kurulamazsa sunucu mount
# edilmez ama API'nin geri kalanı normal çalışır.
_mcp_asgi_app = None
_mcp_session_manager = None
try:
    from app.tools.mcp_app import build_mcp_asgi_app

    _mcp_asgi_app, _mcp_session_manager = build_mcp_asgi_app()
except Exception as _mcp_exc:  # pragma: no cover — opsiyonel bağımlılık
    print(f"⚠️  MCP server unavailable: {_mcp_exc}")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application startup and shutdown events."""
    # Startup
    print(f"🚀 {settings.app_name} v{settings.app_version} starting...")

    if settings.is_development:
        print("🛢️  [DEV] Creating database tables...")
        await init_db()
        print("✅ [DEV] Database tables ready.")

    # v2 güvenlik (Fix #2): her backend başlangıcında REAL hesap arming'i
    # KOŞULSUZ düşürülür. Restart, hesap/oturum kimliğinin sessizce değişmiş
    # olabileceği bir andır; operatör her açılışta yeniden arm etmelidir.
    # Disarm BAŞARISIZ olursa fail-closed: dispatch süreç boyunca sert
    # bloklanır (scanner başlar ama emir göndermez — OBSERVE_ONLY davranışı).
    try:
        from app.db.session import async_session_factory
        from app.services.admin_config import (
            disarm_real_account,
            get_admin_config_value,
            _parse_bool,
        )

        async with async_session_factory() as _session:
            if _parse_bool(await get_admin_config_value(_session, "realAccountArmed")):
                await disarm_real_account(
                    _session,
                    "startup unconditional disarm",
                    changed_by="SYSTEM_STARTUP",
                )
                await _session.commit()
                print("🔒 REAL account disarmed on startup (re-arm required).")
    except Exception as _disarm_exc:
        from app.core.logger import log_runtime_event
        from app.core.runtime_flags import block_dispatch
        from app.services.notifications import notify_error

        _block_reason = f"startup disarm failed: {_disarm_exc}"
        block_dispatch(_block_reason)
        log_runtime_event(event_type="DISPATCH_HARD_BLOCKED", detail=_block_reason)
        # Bu latch süreç boyunca TÜM emirleri sessizce durdurur ve ancak
        # restart ile temizlenir; operatör hemen haberdar olmalı.
        await notify_error(
            "Dispatch hard-blocked on startup (fail-closed)",
            {"reason": _block_reason},
        )
        print(
            f"⛔ Startup disarm FAILED — dispatch hard-blocked (fail-closed): "
            f"{_disarm_exc}"
        )

    if settings.scanner_enabled:
        from app.services.scanner import scanner

        scanner.start()
        print("🔍 Scanner started; dispatch remains gated by systemMode and preflight.")

    if settings.position_sync_enabled:
        from app.services.position_sync import position_synchronizer

        position_synchronizer.start()

    if settings.order_sync_enabled:
        from app.services.order_sync import order_synchronizer

        order_synchronizer.start()

    async with AsyncExitStack() as stack:
        if _mcp_session_manager is not None:
            await stack.enter_async_context(_mcp_session_manager.run())
            print("🧰 MCP read-only tool server mounted at /mcp.")
        yield
    # Shutdown
    if settings.scanner_enabled:
        from app.services.scanner import scanner

        await scanner.stop()
    if settings.position_sync_enabled:
        from app.services.position_sync import position_synchronizer

        await position_synchronizer.stop()
    if settings.order_sync_enabled:
        from app.services.order_sync import order_synchronizer

        await order_synchronizer.stop()
    print(f"👋 {settings.app_name} shutting down...")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Trade AI Server — modular FastAPI backend for AI-powered trading",
    lifespan=lifespan,
)

# Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ────────────────────────────────────────────────────────────────

# Public (no auth)
app.include_router(health_router, prefix="/api")
app.include_router(gateway_config_router, prefix="/api")

# Protected (Bearer token required)
app.include_router(signal_router, prefix="/api")
app.include_router(order_result_router, prefix="/api")
app.include_router(gateway_log_router, prefix="/api")
app.include_router(signal_history_router, prefix="/api")
app.include_router(admin_api_router, prefix="/api/admin")
app.include_router(admin_router, prefix="/admin")

# MCP (admin token korumalı, read-only tool yüzeyi)
if _mcp_asgi_app is not None:
    app.mount("/mcp", _mcp_asgi_app)


@app.get("/")
async def root() -> dict[str, str]:
    """Root redirect to API docs."""
    return {
        "message": f"Welcome to {settings.app_name}",
        "docs": "/docs",
        "health": "/api/health",
    }
