"""Database initialisation — table creation for dev mode.

In production, use Alembic migrations instead. For dev mode the server
auto-creates tables on startup when ``APP_ENV=development``.
"""

from __future__ import annotations

import importlib

from app.db.base import Base
from app.db.session import async_session_factory, engine


async def init_db() -> None:
    """Create all tables if they don't exist yet.

    Relies on the models being imported (via ``app.models.db``) so that
    SQLAlchemy's ``Base.metadata`` knows about every ORM class.
    """
    # Ensure all model modules are loaded so Base.metadata is populated.
    importlib.import_module("app.models.db")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from app.services.trade_profile import ensure_builtin_profiles_seeded

    async with async_session_factory() as session:
        await ensure_builtin_profiles_seeded(session)


async def drop_all() -> None:
    """Drop all tables — **DEV ONLY**. Used by test fixtures."""
    importlib.import_module("app.models.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
