from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import pytest
from psycopg.types.json import Jsonb
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from stack_check.stack_check import AsyncQuerier


DATABASE_URL = os.environ.get("STACK_CHECK_DATABASE_URL")
MIGRATION = Path(__file__).parents[1] / "db/migrations/0001_stack_check.sql"


@pytest.fixture(scope="module")
def db_engine() -> Iterator[AsyncEngine]:
    if not DATABASE_URL:
        pytest.skip("STACK_CHECK_DATABASE_URL is required for PostgreSQL integration checks")

    # Tests use separate asyncio.run() loops; don't retain pooled connections
    # across those loops.
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)

    async def migrate() -> None:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(MIGRATION.read_text())

    asyncio.run(migrate())
    try:
        yield engine
    finally:
        asyncio.run(engine.dispose())


def record_values() -> tuple[uuid.UUID, Decimal, dict[str, Any], dt.datetime]:
    return (
        uuid.uuid4(),
        Decimal("1234567890123456.78"),
        {"source": "stack-check", "attributes": ["uuid", "jsonb"]},
        dt.datetime(2026, 10, 24, 9, 30, 15, 123456, tzinfo=dt.timezone.utc),
    )


def test_sqlc_generated_query_round_trips_postgres_types(db_engine: AsyncEngine) -> None:
    id_, amount, metadata, created_at = record_values()

    async def run() -> tuple[Any, Any]:
        async with db_engine.begin() as conn:
            record = await AsyncQuerier(conn).create_stack_check_record(
                id=id_, amount=amount, metadata=Jsonb(metadata), created_at=created_at
            )

        async with db_engine.connect() as conn:
            persisted = await AsyncQuerier(conn).get_stack_check_record(id=id_)
        return record, persisted

    record, persisted = asyncio.run(run())

    assert record is not None
    assert persisted is not None
    assert isinstance(record.amount, Decimal)
    assert isinstance(persisted.amount, Decimal)
    assert persisted.id == id_
    assert persisted.amount == amount
    assert persisted.metadata == metadata
    assert persisted.created_at == created_at
    assert persisted.created_at.tzinfo is not None
    assert persisted.created_at.utcoffset() == dt.timedelta(0)


def test_generated_write_is_rolled_back_with_outer_transaction(db_engine: AsyncEngine) -> None:
    id_, amount, metadata, created_at = record_values()

    async def run() -> Any:
        with pytest.raises(RuntimeError, match="force rollback"):
            async with db_engine.begin() as conn:
                await AsyncQuerier(conn).create_stack_check_record(
                    id=id_, amount=amount, metadata=Jsonb(metadata), created_at=created_at
                )
                raise RuntimeError("force rollback")

        async with db_engine.connect() as conn:
            return await AsyncQuerier(conn).get_stack_check_record(id=id_)

    record = asyncio.run(run())

    assert record is None
