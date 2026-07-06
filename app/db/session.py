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

DATABASE_URL = settings.database_url or "sqlite+aiosqlite:///./trade_ai.db"

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
