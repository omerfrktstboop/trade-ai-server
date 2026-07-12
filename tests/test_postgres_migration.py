from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.order_ledger import reserve_order


POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="requires isolated TEST_POSTGRES_URL"
)


def _alembic(*arguments: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        check=True,
        env={**os.environ, "DATABASE_URL": POSTGRES_URL},
    )


async def _prepare_legacy_schema() -> None:
    engine = create_async_engine(POSTGRES_URL)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
        await connection.execute(
            text(
                """
                CREATE TABLE order_logs (
                    id SERIAL PRIMARY KEY,
                    request_id VARCHAR(64) NOT NULL,
                    symbol VARCHAR(16) NOT NULL,
                    action VARCHAR(8) NOT NULL,
                    qty DOUBLE PRECISION,
                    price DOUBLE PRECISION,
                    order_id VARCHAR(64),
                    status VARCHAR(32),
                    mode VARCHAR(16),
                    matrix_message TEXT,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                INSERT INTO order_logs
                    (request_id, symbol, action, qty, price, status, matrix_message)
                VALUES
                    ('duplicate-1', 'THYAO', 'BUY', 1, 100, 'SENT_PENDING', 'sent'),
                    ('duplicate-1', 'THYAO', 'BUY', 2, 100, 'FILLED', 'filled')
                """
            )
        )
    await engine.dispose()


async def _verify_upgrade_and_concurrent_upsert() -> None:
    engine = create_async_engine(POSTGRES_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.connect() as connection:
        duplicate_count = await connection.scalar(
            text("SELECT count(*) FROM order_logs WHERE request_id='duplicate-1'")
        )
        merged = (
            await connection.execute(
                text(
                    "SELECT status, state, order_qty FROM order_logs "
                    "WHERE request_id='duplicate-1'"
                )
            )
        ).one()
        assert duplicate_count == 1
        assert merged.status == "FILLED"
        assert merged.state == "FILLED"
        assert merged.order_qty == 2
        with pytest.raises(IntegrityError):
            await connection.execute(
                text(
                    "INSERT INTO order_logs "
                    "(request_id, symbol, action) VALUES "
                    "('duplicate-1', 'THYAO', 'BUY')"
                )
            )

    async def reserve():
        async with factory() as session:
            return await reserve_order(
                session,
                request_id="concurrent-1",
                symbol="AKBNK",
                side="BUY",
                qty=1,
                limit_price=50,
                mode="DEMO_LIVE",
            )

    results = await asyncio.gather(reserve(), reserve())
    assert sorted(result[1] for result in results) == [False, True]
    await engine.dispose()


async def _verify_rollback() -> None:
    engine = create_async_engine(POSTGRES_URL)
    async with engine.connect() as connection:
        columns = {
            row[0]
            for row in (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='order_logs'"
                    )
                )
            )
        }
        assert "request_fingerprint" not in columns
        assert "order_qty" not in columns
    await engine.dispose()


def test_postgresql_upgrade_cleanup_unique_rollback_and_upsert():
    asyncio.run(_prepare_legacy_schema())
    _alembic("upgrade", "head")
    asyncio.run(_verify_upgrade_and_concurrent_upsert())
    _alembic("downgrade", "base")
    asyncio.run(_verify_rollback())
    _alembic("upgrade", "head")
