"""Checksummed, advisory-lock protected SQL migrations for PostgreSQL."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

LOG = logging.getLogger(__name__)
DEFAULT_MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations" / "app"
DATABASE_URL_ENV = "DATABASE_URL"
_MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]{4,})_[a-z0-9][a-z0-9_]*\.sql$")
_ADVISORY_LOCK_ID = 0x435245444954  # ASCII-ish "CREDIT", stable across processes.


class MigrationError(RuntimeError):
    """Database schema is missing, drifted, or could not be migrated safely."""


def migration_files(directory: Path = DEFAULT_MIGRATIONS) -> list[tuple[int, Path, bytes]]:
    files: list[tuple[int, Path, bytes]] = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            continue
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise MigrationError(f"Cannot read migration {path.name}") from exc
        if not content.strip():
            raise MigrationError(f"Migration {path.name} is empty")
        files.append((int(match.group("version")), path, content))
    files.sort(key=lambda item: item[0])
    versions = [version for version, _, _ in files]
    if len(versions) != len(set(versions)):
        raise MigrationError("Migration filenames contain duplicate versions")
    if not files:
        raise MigrationError(f"No production migrations found in {directory}")
    return files


def migration_manifest(directory: Path = DEFAULT_MIGRATIONS) -> dict[int, tuple[str, str]]:
    """Return version -> filename/checksum for the current source tree."""
    return {
        version: (path.name, hashlib.sha256(content).hexdigest())
        for version, path, content in migration_files(directory)
    }


async def apply_migrations(
    database_url: str,
    directory: Path = DEFAULT_MIGRATIONS,
    *,
    engine=None,
) -> None:
    """Apply pending migrations, or fail on source drift / unknown DB versions.

    Each SQL file and its ledger entry commit together. A session advisory lock
    serializes simultaneous `migrate` commands. The runtime API and worker call
    readiness checks only; they do not apply DDL automatically.
    """
    migrations = migration_files(directory)
    owns_engine = engine is None
    db_engine = engine or create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with db_engine.connect() as conn:
            await conn.execute(text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": _ADVISORY_LOCK_ID})
            await conn.commit()
            try:
                await _create_ledger(conn)
                await _assert_ledger_matches(conn, migrations)
                for version, path, content in migrations:
                    digest = hashlib.sha256(content).hexdigest()
                    applied = await conn.scalar(
                        text("SELECT 1 FROM schema_migrations WHERE version = :version"),
                        {"version": version},
                    )
                    await conn.commit()
                    if applied:
                        continue
                    # Do not accept an out-of-order migration: a developer may
                    # be deploying an older image to a database with a newer schema.
                    current = await conn.scalar(text("SELECT COALESCE(MAX(version), 0) FROM schema_migrations"))
                    await conn.commit()
                    if version <= int(current or 0):
                        raise MigrationError(f"Migration {path.name} is older than the database schema")
                    async with conn.begin():
                        await conn.exec_driver_sql(
                            content.decode("utf-8"),
                            execution_options={"no_parameters": True},
                        )
                        await conn.execute(
                            text("""
                                INSERT INTO schema_migrations (version, filename, checksum)
                                VALUES (:version, :filename, :checksum)
                            """),
                            {"version": version, "filename": path.name, "checksum": digest},
                        )
                    LOG.info("Applied database migration", extra={"migration": path.name, "version": version})
                await _assert_ledger_matches(conn, migrations)
            finally:
                if conn.in_transaction():
                    await conn.rollback()
                await conn.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": _ADVISORY_LOCK_ID})
                await conn.commit()
    finally:
        if owns_engine:
            await db_engine.dispose()


async def _create_ledger(conn: AsyncConnection) -> None:
    async with conn.begin():
        await conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version BIGINT PRIMARY KEY,
                filename TEXT NOT NULL UNIQUE,
                checksum CHAR(64) NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)


async def _assert_ledger_matches(
    conn: AsyncConnection,
    migrations: Iterable[tuple[int, Path, bytes]],
) -> None:
    rows = (await conn.execute(text("SELECT version, filename, checksum FROM schema_migrations ORDER BY version"))).all()
    await conn.commit()
    source = {
        version: (path.name, hashlib.sha256(content).hexdigest())
        for version, path, content in migrations
    }
    for row in rows:
        version = int(row.version)
        current = source.get(version)
        if current is None:
            raise MigrationError(f"Database has unknown migration version {version}; current source does not contain it")
        if (row.filename, row.checksum.strip()) != current:
            raise MigrationError(f"Applied migration {version} does not match its current filename and checksum")


async def migrations_current(database_url: str, directory: Path = DEFAULT_MIGRATIONS, *, engine=None) -> bool:
    """True only when every source migration was applied with its exact checksum."""
    try:
        source = migration_manifest(directory)
    except (OSError, MigrationError):
        return False
    owns_engine = engine is None
    db_engine = engine or create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with db_engine.connect() as conn:
            rows = (await conn.execute(text("SELECT version, filename, checksum FROM schema_migrations ORDER BY version"))).all()
        database = {int(row.version): (row.filename, row.checksum.strip()) for row in rows}
        return database == source
    except Exception:
        return False
    finally:
        if owns_engine:
            await db_engine.dispose()


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    database_url = os.getenv(DATABASE_URL_ENV)
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=DEFAULT_MIGRATIONS)
    args = parser.parse_args()
    await apply_migrations(database_url, args.directory)


if __name__ == "__main__":
    asyncio.run(main())
