"""Tests for the explicit production database initialisation command."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from scripts.init_db_once import DatabaseInitError, initialize_database


@pytest.mark.asyncio
async def test_init_requires_explicit_yes_and_does_not_create_db(tmp_path):
    database_path = tmp_path / "not-created.db"

    with pytest.raises(DatabaseInitError, match="explicit approval"):
        await initialize_database(
            database_url=f"sqlite+aiosqlite:///{database_path}",
            app_env="development",
            yes=False,
        )

    assert not database_path.exists()


@pytest.mark.asyncio
async def test_dry_run_does_not_call_create_all(monkeypatch, tmp_path):
    create_all = Mock()
    monkeypatch.setattr(Base.metadata, "create_all", create_all)

    tables = await initialize_database(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'dry-run.db'}",
        app_env="development",
        yes=False,
        dry_run=True,
    )

    assert "manual_approval_requests" in tables
    assert "position_management_decisions" in tables
    assert "watchlist_quality_scores" in tables
    assert "research_candidates" in tables
    assert "trade_watchlist_symbols" in tables
    create_all.assert_not_called()


@pytest.mark.asyncio
async def test_yes_creates_schema_seeds_profiles_and_is_idempotent(tmp_path):
    database_path = tmp_path / "init.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    for _ in range(2):
        await initialize_database(
            database_url=f"sqlite+aiosqlite:///{database_path}",
            app_env="development",
            yes=True,
            engine=engine,
            session_factory=factory,
        )

    async with engine.connect() as connection:
        for table in (
            "manual_approval_requests",
            "position_management_decisions",
            "watchlist_quality_scores",
            "ai_lessons_learned",
            "trade_profiles",
            "watchlist_symbols",
            "research_candidates",
            "research_candidate_events",
            "trade_watchlist_symbols",
        ):
            result = await connection.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=:name"
                ),
                {"name": table},
            )
            assert result.scalar_one() == table
        profiles = await connection.execute(text("SELECT count(*) FROM trade_profiles"))
        assert profiles.scalar_one() == 4

    assert "ManualApprovalRequest" not in Base.metadata.tables
    assert "manual_approval_requests" in Base.metadata.tables
    assert "position_management_decisions" in Base.metadata.tables
    assert "watchlist_quality_scores" in Base.metadata.tables
    assert "research_candidates" in Base.metadata.tables
    assert "trade_watchlist_symbols" in Base.metadata.tables
    await engine.dispose()


@pytest.mark.asyncio
async def test_production_rejects_sqlite(tmp_path):
    with pytest.raises(DatabaseInitError, match="PostgreSQL"):
        await initialize_database(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'production.db'}",
            app_env="production",
            yes=True,
        )
