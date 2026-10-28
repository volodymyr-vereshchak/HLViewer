"""Every migration can be walked down and back up.

The admin updater rolls a failed update back with `alembic downgrade` to the
revision the old code expects, and only falls back to restoring the pre-update
dump when that fails. A downgrade that was written but never run is the thing
that would fail there, on the server, in the middle of an outage — so the
whole chain is exercised here: base → head → base → head.

It runs in a database of its own: the shared test database is built from the
models, not from the migrations, and has no alembic_version to walk.
"""
import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest

BACKEND = Path(__file__).parents[2] / "backend"
DB_NAME = "hostlib_migrations_test"


async def _admin_connection():
    return await asyncpg.connect(
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        database="postgres",
    )


@pytest.fixture
async def migrations_db():
    conn = await _admin_connection()
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{DB_NAME}"')
    finally:
        await conn.close()
    yield DB_NAME
    conn = await _admin_connection()
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
    finally:
        await conn.close()


def _alembic(db_name: str, *args: str) -> None:
    env = {**os.environ, "POSTGRES_DB": db_name}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
    )


async def test_upgrade_downgrade_upgrade(migrations_db):
    _alembic(migrations_db, "upgrade", "head")
    _alembic(migrations_db, "downgrade", "base")
    _alembic(migrations_db, "upgrade", "head")
