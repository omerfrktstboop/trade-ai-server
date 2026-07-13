from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text


def test_task1b_sqlite_migration_upgrade_and_downgrade(tmp_path: Path):
    database = tmp_path / "task1b-test.db"
    sync_url = f"sqlite:///{database.as_posix()}"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{database.as_posix()}",
        "APP_ENV": "development",
    }
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
    engine.dispose()

    def alembic(*args: str) -> None:
        subprocess.run([sys.executable, "-m", "alembic", *args], check=True, env=env)

    alembic("upgrade", "head")
    engine = create_engine(sync_url)
    inspector = inspect(engine)
    assert "order_cash_reservations" in inspector.get_table_names()
    assert "account_normalization_audits" in inspector.get_table_names()
    assert "account_reservation_scopes" in inspector.get_table_names()
    profile_columns = {item["name"] for item in inspector.get_columns("trade_profiles")}
    assert "allow_margin_buying" in profile_columns
    unique = inspector.get_unique_constraints("order_cash_reservations")
    assert any(item["column_names"] == ["request_id"] for item in unique)
    engine.dispose()

    alembic("downgrade", "20260713_02")
    engine = create_engine(sync_url)
    inspector = inspect(engine)
    assert "order_cash_reservations" not in inspector.get_table_names()
    assert "account_normalization_audits" not in inspector.get_table_names()
    profile_columns = {item["name"] for item in inspector.get_columns("trade_profiles")}
    assert "allow_margin_buying" not in profile_columns
    engine.dispose()

    alembic("upgrade", "head")
