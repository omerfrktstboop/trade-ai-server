from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect


def test_research_watchlist_migration_upgrade_and_downgrade(tmp_path: Path):
    database = tmp_path / "research-migration-test.db"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{database.as_posix()}",
        "APP_ENV": "development",
    }

    def alembic(*args: str) -> None:
        subprocess.run([sys.executable, "-m", "alembic", *args], check=True, env=env)

    alembic("upgrade", "head")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "research_candidates" in tables
    assert "research_candidate_events" in tables
    assert "trade_watchlist_symbols" in tables
    assert any(
        item["column_names"] == ["symbol"]
        for item in inspector.get_unique_constraints("research_candidates")
    )
    assert any(
        item["column_names"] == ["symbol"]
        for item in inspector.get_unique_constraints("trade_watchlist_symbols")
    )
    engine.dispose()

    alembic("downgrade", "20260713_04")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    tables = set(inspect(engine).get_table_names())
    assert "research_candidates" not in tables
    assert "research_candidate_events" not in tables
    assert "trade_watchlist_symbols" not in tables
    engine.dispose()

    alembic("upgrade", "head")
