"""
Global test bootstrap for the HLViewer backend suite.

CRITICAL IMPORT ORDER
=====================
`backend/db/engine.py` builds the Postgres connection URL from env vars **at
import time** (module-level `DbEngine()` singleton). Therefore every env
override below MUST happen before the first `backend.*` import — otherwise the
app engine would silently point at the developer/production database.

Isolation model
===============
- One test database (`hostlib_test_db` by default) is (re)created per session:
  schema is dropped and rebuilt from `SQLModel.metadata` (fixture `_test_database`).
- Application code commits through the global `async_session_factory`, so
  transactional rollback isolation is impossible. Instead, the `clean_db`
  fixture TRUNCATEs all tables before each test that asks for it (integration
  tests do so automatically via an autouse wrapper in tests/integration/conftest.py).
- Unit tests (tests/unit/) never touch the database: `_test_database` is NOT
  autouse, it is only pulled in through the `clean_db` dependency chain.
"""

import asyncio
import os
import struct
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

# 1) Local dev convenience: reuse the dockerized Postgres credentials/host/port
#    from .env (no-op in CI where the file doesn't exist). load_dotenv never
#    overrides variables that are already set in the environment.
load_dotenv(ROOT / ".env")

# 2) Force the TEST database and a safe auth env (override anything loaded above).
os.environ["POSTGRES_DB"] = os.getenv("TEST_POSTGRES_DB", "hostlib_test_db")
os.environ["AUTO_LOGIN"] = "false"        # .env may enable it; tests need the login flow
os.environ["LDAP_ENABLED"] = "false"      # .env may enable it; LDAP tests opt in explicitly
os.environ["COOKIE_SECURE"] = "false"
os.environ.setdefault("JWT_SECRET", "test-only-secret-not-for-production")
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "5434")
os.environ.setdefault("POSTGRES_USER", "postgres")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres")

# 3) Fail-closed guard: never run the suite against a non-test database.
_TEST_DB = os.environ["POSTGRES_DB"]
if "test" not in _TEST_DB.lower():
    raise RuntimeError(
        f"Refusing to run tests against database '{_TEST_DB}': the name must "
        "contain 'test' (set TEST_POSTGRES_DB to override the default)."
    )

# ── Only now is it safe to import application modules ─────────────────────────
import asyncpg  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import URL  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402

from backend.api.main import app  # noqa: E402  (imports every router → every model)
from backend.api.endpoints.auth_ep import hash_password  # noqa: E402
from backend.db.engine import _db, async_session_factory  # noqa: E402
from backend.db.models.app_user_model import AppUser  # noqa: E402


# ── Test users (seeded per test via seed_users) ───────────────────────────────
ADMIN_USERNAME = "testadmin"
ADMIN_PASSWORD = "admin-pass-123"
VIEWER_USERNAME = "testviewer"
VIEWER_PASSWORD = "viewer-pass-123"


def _pg_conn_kwargs(database: str) -> dict:
    return dict(
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        database=database,
    )


def _test_db_url() -> URL:
    return URL.create(
        "postgresql+asyncpg",
        username=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        database=_TEST_DB,
    )


async def _ensure_database() -> None:
    """Create the test database if it does not exist yet."""
    conn = await asyncpg.connect(**_pg_conn_kwargs("postgres"))
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", _TEST_DB
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{_TEST_DB}"')
    finally:
        await conn.close()


async def _recreate_schema() -> None:
    """Drop and re-create all tables from the current SQLModel metadata.

    Uses a throwaway engine (own event loop via asyncio.run) so the app engine's
    pooled connections are never bound to a foreign loop.
    """
    engine = create_async_engine(_test_db_url())
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.drop_all)
            await conn.run_sync(SQLModel.metadata.create_all)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def _test_database():
    """Session-scoped: test DB exists and has a fresh schema. Sync on purpose —
    runs in its own asyncio.run loops, independent of pytest-asyncio's loop."""
    asyncio.run(_ensure_database())
    asyncio.run(_recreate_schema())
    yield


@pytest_asyncio.fixture
async def clean_db(_test_database):
    """TRUNCATE every table before the test. All DB-touching fixtures depend on
    this, so each test starts from an empty, identity-reset database.

    Also disposes the app engine pools first: bulk_upsert_via_copy re-creates
    temp tables with positional c0..cN columns whose types depend on the
    caller's dict-key order, and asyncpg's per-connection prepared-statement
    cache would otherwise serve stale column types when a pooled connection is
    reused by a test with a different key order."""
    await _db.engine.dispose()
    await _db.update_engine.dispose()
    tables = ", ".join(
        f'"{t.name}"' for t in reversed(SQLModel.metadata.sorted_tables)
    )
    async with async_session_factory() as session:
        await session.execute(
            text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
        )
        await session.commit()
    yield


# ── HTTP clients ──────────────────────────────────────────────────────────────
def _make_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


# bcrypt is deliberately slow — hash the fixed test passwords once per session.
@pytest.fixture(scope="session")
def _admin_hash() -> str:
    return hash_password(ADMIN_PASSWORD)


@pytest.fixture(scope="session")
def _viewer_hash() -> str:
    return hash_password(VIEWER_PASSWORD)


@pytest_asyncio.fixture
async def seed_users(clean_db, _admin_hash, _viewer_hash) -> dict:
    """Insert the standard admin + viewer accounts. Returns {'admin': id, 'viewer': id}."""
    async with async_session_factory() as session:
        admin = AppUser(
            username=ADMIN_USERNAME, role="admin", active=True, password_hash=_admin_hash
        )
        viewer = AppUser(
            username=VIEWER_USERNAME, role="viewer", active=True, password_hash=_viewer_hash
        )
        session.add(admin)
        session.add(viewer)
        await session.commit()
        await session.refresh(admin)
        await session.refresh(viewer)
        return {"admin": admin.id, "viewer": viewer.id}


@pytest_asyncio.fixture
async def anon_client(clean_db):
    """Unauthenticated client (no cookie)."""
    async with _make_client() as client:
        yield client


async def _login(client: AsyncClient, username: str, password: str) -> None:
    resp = await client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, f"login failed for {username}: {resp.text}"


@pytest_asyncio.fixture
async def admin_client(seed_users):
    """Client logged in as the seeded admin (cookie kept in the jar)."""
    async with _make_client() as client:
        await _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
        yield client


@pytest_asyncio.fixture
async def viewer_client(seed_users):
    """Client logged in as the seeded viewer."""
    async with _make_client() as client:
        await _login(client, VIEWER_USERNAME, VIEWER_PASSWORD)
        yield client


# ── Shared data fixtures ──────────────────────────────────────────────────────
@pytest.fixture
def sample_binary_data() -> bytes:
    """One HourStruct record (`=5B6f`): month, day, year, hour, minutes,
    volume, unknown, w_volume_dp, pressure, temperature, density."""
    return struct.pack(
        "=5B6f", 12, 25, 24, 14, 30, 1000.5, 0.0, 0.1, 5.2, 20.5, 0.7
    )


@pytest.fixture
def sample_archive_file(tmp_path, sample_binary_data) -> str:
    """Binary archive file with 10 identical HourStruct records."""
    path = tmp_path / "test_archive.bin"
    path.write_bytes(sample_binary_data * 10)
    return str(path)


@pytest.fixture
def sample_hourly_data() -> dict:
    return {
        "period": datetime(2024, 12, 25, 14, 30),
        "volume": 1000.5,
        "w_volume_dp": 0.1,
        "pressure": 5.2,
        "temperature": 20.5,
        "density": 0.7,
        "line_id": 1,
    }


@pytest.fixture
def sample_daily_data() -> dict:
    return {
        "period": datetime(2024, 12, 25),
        "volume": 24000.0,
        "w_volume_dp": 2.4,
        "pressure": 5.2,
        "temperature": 20.5,
        "density": 0.7,
        "line_id": 1,
    }
