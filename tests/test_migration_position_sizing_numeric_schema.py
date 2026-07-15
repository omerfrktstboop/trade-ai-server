from __future__ import annotations

import os
import subprocess
import sys

from sqlalchemy import create_engine, inspect, text


def test_sqlite_migration_upgrade_rollback_and_numeric_schema(tmp_path):
    database = tmp_path / "task1a_test.db"
    sync_url = f"sqlite:///{database.as_posix()}"
    async_url = f"sqlite+aiosqlite:///{database.as_posix()}"
    env = {**os.environ, "DATABASE_URL": async_url, "APP_ENV": "development"}
    engine = create_engine(sync_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE trade_profiles (
                    id INTEGER PRIMARY KEY,
                    max_order_value_tl FLOAT NOT NULL,
                    max_qty_per_order FLOAT NOT NULL,
                    max_position_value_per_symbol FLOAT NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                "INSERT INTO trade_profiles VALUES "
                "(1, 1000.12345678, 3.9, 3000.12345678)"
            )
        )
    engine.dispose()

    def alembic(*args: str) -> None:
        subprocess.run([sys.executable, "-m", "alembic", *args], check=True, env=env)

    alembic("upgrade", "head")
    engine = create_engine(sync_url)
    assert "position_sizing_audits" in inspect(engine).get_table_names()
    numeric = {
        column["name"]: str(column["type"])
        for column in inspect(engine).get_columns("position_sizing_audits")
    }
    assert numeric["risk_budget_tl"].startswith("NUMERIC")
    profile_types = {
        column["name"]: str(column["type"])
        for column in inspect(engine).get_columns("trade_profiles")
    }
    assert profile_types["max_order_value_tl"].startswith("NUMERIC")
    assert profile_types["max_qty_per_order"] == "INTEGER"
    engine.dispose()

    alembic("downgrade", "20260712_01")
    engine = create_engine(sync_url)
    assert "position_sizing_audits" not in inspect(engine).get_table_names()
    engine.dispose()
    alembic("upgrade", "head")
