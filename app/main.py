"""FastAPI application entry point."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db.init_db import init_db
from app.routers.health import router as health_router
from app.routers.order_result import router as order_result_router
from app.routers.signal import router as signal_router
from app.routers.signals import router as signals_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application startup and shutdown events."""
    # Startup
    print(f"🚀 {settings.app_name} v{settings.app_version} starting...")

    if settings.is_development:
        print("🛢️  [DEV] Creating database tables...")
        await init_db()
        print("✅ [DEV] Database tables ready.")

    yield
    # Shutdown
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

# Protected (Bearer token required)
app.include_router(signal_router, prefix="/api")
app.include_router(order_result_router, prefix="/api")
app.include_router(signals_router, prefix="/api")


@app.get("/")
async def root() -> dict[str, str]:
    """Root redirect to API docs."""
    return {
        "message": f"Welcome to {settings.app_name}",
        "docs": "/docs",
        "health": "/api/health",
    }
