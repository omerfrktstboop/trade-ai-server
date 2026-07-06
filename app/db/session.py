"""Async SQLAlchemy engine and session factory.

Provides ``get_async_session`` as a FastAPI dependency and
``engine`` for direct access (table creation, etc.).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

# Development: SQLite fallback when DATABASE_URL is empty.
# Production: DATABASE_URL must be set (enforced by config validator).
if settings.is_production:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required in production.")
    if settings.database_url.startswith("sqlite"):
        raise RuntimeError("SQLite is not allowed in production — use PostgreSQL.")

DATABASE_URL = settings.database_url or "sqlite+aiosqlite:///./dev.db"

engine = create_async_engine(
    DATABASE_URL,
    echo=settings.debug,
    future=True,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields an async session, closed after the request."""
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()
