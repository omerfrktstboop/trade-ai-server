"""Explicit, idempotent database initialisation for first deployments.

This module is deliberately not imported by the FastAPI lifespan.  It is for
an operator to run once after configuring DATABASE_URL, before starting the
server in production.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import re
from collections.abc import Sequence
from typing import Any

from app.config import AppEnv, settings
from app.db.base import Base

logger = logging.getLogger(__name__)


class DatabaseInitError(RuntimeError):
    """Raised when an explicit database initialisation is unsafe."""


def database_kind(database_url: str) -> str:
    """Return the SQLAlchemy scheme family without exposing credentials."""
    return database_url.split(":", 1)[0].split("+", 1)[0].lower()


def mask_database_url(database_url: str) -> str:
    """Mask credentials (and SQLite paths) before writing a log message."""
    if database_url.lower().startswith("sqlite"):
        return f"{database_url.split(':', 1)[0]}:///***"
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", database_url)


def table_names() -> list[str]:
    """Import every ORM model and return the tables which would be created."""
    importlib.import_module("app.models.db")
    return [table.name for table in Base.metadata.sorted_tables]


def _validate_target(database_url: str, app_env: AppEnv | str) -> None:
    if not database_url.strip():
        raise DatabaseInitError("DATABASE_URL is required; refusing to use a fallback database.")
    environment = app_env.value if isinstance(app_env, AppEnv) else str(app_env).lower()
    if environment == AppEnv.PRODUCTION.value and database_kind(database_url) != "postgresql":
        raise DatabaseInitError("Production database initialisation requires a PostgreSQL DATABASE_URL.")


async def initialize_database(
    *,
    database_url: str,
    app_env: AppEnv | str,
    yes: bool,
    dry_run: bool = False,
    engine: Any | None = None,
    session_factory: Any | None = None,
) -> list[str]:
    """Create missing schema objects and seed built-in profiles.

    Dependency parameters exist for isolated SQLite tests.  Normal CLI use
    obtains the application engine and session factory only after all safety
    checks have passed, so omitting ``--yes`` cannot touch the database.
    """
    if not dry_run and not yes:
        raise DatabaseInitError(
            "Refusing to initialise the database without explicit approval. "
            "Run: python -m scripts.init_db_once --yes"
        )

    _validate_target(database_url, app_env)
    tables = table_names()
    logger.info(
        "Database init target: type=%s url=%s", database_kind(database_url), mask_database_url(database_url)
    )
    logger.info("Tables to ensure: %s", ", ".join(tables))

    if dry_run:
        return tables
    if engine is None or session_factory is None:
        from app.db.session import async_session_factory, engine as app_engine

        engine = engine or app_engine
        session_factory = session_factory or async_session_factory

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    from app.services.trade_profile import ensure_builtin_profiles_seeded

    async with session_factory() as session:
        await ensure_builtin_profiles_seeded(session)

    logger.info("Database initialisation completed successfully.")
    return tables


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Explicitly initialise trade-ai-server database tables.")
    choices = parser.add_mutually_exclusive_group()
    choices.add_argument("--yes", action="store_true", help="Confirm create_all and profile seeding.")
    choices.add_argument("--dry-run", action="store_true", help="Show target and tables without touching the database.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    try:
        asyncio.run(
            initialize_database(
                database_url=settings.database_url,
                app_env=settings.app_env,
                yes=args.yes,
                dry_run=args.dry_run,
            )
        )
    except DatabaseInitError as exc:
        logger.error("%s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
