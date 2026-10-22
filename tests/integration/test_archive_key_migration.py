"""The one-time cleanup in migration 0a148d391231, run on duplicates shaped
like the ones prod held on 22.10.2026.

The test schema is built from the models, so it already has the new
(line_id, period) key. The test lifts it, plants the duplicates the old key
allowed, runs the migration's own SQL and puts the key back — which only
succeeds if exactly one row per (line_id, period) is left.
"""
import importlib.util
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from backend.db.engine import async_session_factory

MIGRATION = (
    Path(__file__).parents[2]
    / "backend/db/alembic/versions/0a148d391231_archive_key_line_period.py"
)
MARKER = 1.401298464324817e-45


def _load_migration():
    spec = importlib.util.spec_from_file_location("archive_key_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()


async def _execute(statements, params=None):
    async with async_session_factory() as session:
        for statement in statements:
            await session.execute(text(statement), params or {})
        await session.commit()


@pytest.fixture
async def old_key():
    """Both tables without their unique key for the length of one test."""
    for table, (constraint, _) in migration.TABLES.items():
        await _execute([f"ALTER TABLE {table} DROP CONSTRAINT {constraint}"])
    try:
        yield
    finally:
        for table, (constraint, _) in migration.TABLES.items():
            await _execute([
                f"DELETE FROM {table}",
                f"ALTER TABLE {table} ADD CONSTRAINT {constraint} "
                f"UNIQUE (line_id, period)",
            ])


async def _plant(table: str, line_id: int, period, volume: float,
                 pressure: float = 37.3, temperature: float = 20.0) -> None:
    await _execute([
        f"INSERT INTO {table} (line_id, period, volume, w_volume_dp, pressure, "
        f"temperature, density, created_at, updated_at) "
        f"VALUES (:line, :period, :volume, 1.0, :p, :t, 0.76, NOW(), NOW())"
    ], {"line": line_id, "period": period, "volume": volume,
        "p": pressure, "t": temperature})


async def _cleanup(table: str):
    fold = migration.TABLES[table][1]
    await _execute(migration.dedupe_statements(table, fold))
    async with async_session_factory() as session:
        return (await session.execute(text(
            f"SELECT line_id, period, volume, pressure, temperature "
            f"FROM {table} ORDER BY line_id, period"
        ))).all()


async def test_hourly_cleanup(seed_topology, old_key):
    line, other = seed_topology["line1"], seed_topology["line2"]
    h = lambda *a: datetime(*a)

    # export noise: the same hour twice, last float32 bit apart → newest kept
    await _plant("hourly_archive", line, h(2026, 10, 1, 6), 90969.90625)
    await _plant("hourly_archive", line, h(2026, 10, 1, 6), 90969.9140625)
    # clock change 27.10.2024: two real 03:00 hours → summed, pressure averaged
    await _plant("hourly_archive", line, h(2024, 10, 27, 3), 7395.73, pressure=37.2)
    await _plant("hourly_archive", line, h(2024, 10, 27, 3), 7383.11, pressure=37.4)
    # ...and a later export's copy of one of them, which must not be summed
    await _plant("hourly_archive", line, h(2024, 10, 27, 3), 7383.1104, pressure=37.4)
    # clock change with an empty record beside the real one → empty dropped
    await _plant("hourly_archive", other, h(2025, 10, 26, 3), 0.0,
                 pressure=MARKER, temperature=MARKER)
    await _plant("hourly_archive", other, h(2025, 10, 26, 3), 18315.87, pressure=3.3)
    # an ordinary hour whose newest copy is empty → the value survives
    await _plant("hourly_archive", other, h(2026, 9, 20, 11), 41.5)
    await _plant("hourly_archive", other, h(2026, 9, 20, 11), MARKER)
    # untouched single rows
    await _plant("hourly_archive", line, h(2026, 10, 1, 7), 500.0)

    rows = {(r.line_id, r.period): r for r in await _cleanup("hourly_archive")}

    assert len(rows) == 5
    assert rows[(line, h(2026, 10, 1, 6))].volume == 90969.9140625
    dst = rows[(line, h(2024, 10, 27, 3))]
    assert dst.volume == pytest.approx(7395.73 + 7383.1104)
    assert dst.pressure == pytest.approx(37.3)
    dst_empty = rows[(other, h(2025, 10, 26, 3))]
    assert dst_empty.volume == pytest.approx(18315.87)
    assert dst_empty.pressure == pytest.approx(3.3)
    assert rows[(other, h(2026, 9, 20, 11))].volume == 41.5
    assert rows[(line, h(2026, 10, 1, 7))].volume == 500.0


async def test_daily_cleanup_never_sums(seed_topology, old_key):
    """«ВП ДЕЛЬТА», 06.12 and 08.12.2024: one empty day beside a real one,
    in either order. Daily rows have no clock-change hour to fold."""
    line = seed_topology["line1"]
    await _plant("daily_archive", line, date(2024, 12, 6), 0.0, pressure=0.89)
    await _plant("daily_archive", line, date(2024, 12, 6), 1199.41, pressure=6.13)
    await _plant("daily_archive", line, date(2024, 12, 8), 1434.53, pressure=6.11)
    await _plant("daily_archive", line, date(2024, 12, 8), 0.0, pressure=0.89)
    # October noise on a Sunday the 25th+ is still just noise for daily rows
    await _plant("daily_archive", line, date(2026, 10, 25), 96574.0703125)
    await _plant("daily_archive", line, date(2026, 10, 25), 96574.078125)

    rows = {r.period: r for r in await _cleanup("daily_archive")}

    assert rows[date(2024, 12, 6)].volume == pytest.approx(1199.41)
    assert rows[date(2024, 12, 6)].pressure == pytest.approx(6.13)
    assert rows[date(2024, 12, 8)].volume == pytest.approx(1434.53)
    assert rows[date(2026, 10, 25)].volume == 96574.078125


async def test_cleanup_without_duplicates_changes_nothing(seed_topology, old_key):
    line = seed_topology["line1"]
    await _plant("hourly_archive", line, datetime(2024, 10, 27, 3), 14778.84)
    rows = await _cleanup("hourly_archive")
    assert [r.volume for r in rows] == [pytest.approx(14778.84)]


SYS_MIGRATION = (
    Path(__file__).parents[2]
    / "backend/db/alembic/versions/09ed82b9aa99_sys_archive_key_without_volume.py"
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sys_migration = _load(SYS_MIGRATION, "sys_key_migration")


@pytest.fixture
async def old_sys_key():
    await _execute([f"ALTER TABLE sys_archive DROP CONSTRAINT {sys_migration.CONSTRAINT}"])
    try:
        yield
    finally:
        await _execute([
            "DELETE FROM sys_archive",
            f"ALTER TABLE sys_archive ADD CONSTRAINT {sys_migration.CONSTRAINT} "
            f"UNIQUE (line_id, period, sys_type_id)",
        ])


async def _event(line_id: int, period: datetime, sys_type_id: int, volume: float):
    await _execute([
        "INSERT INTO sys_archive (line_id, period, sys_type_id, volume, "
        "created_at, updated_at) VALUES (:l, :p, :t, :v, NOW(), NOW())"
    ], {"l": line_id, "p": period, "t": sys_type_id, "v": volume})


async def test_sys_cleanup(seed_topology, old_sys_key):
    line = seed_topology["line1"]
    noise = datetime(2026, 10, 3, 23, 53, 45)
    await _event(line, noise, 2, 119860.9765625)
    await _event(line, noise, 2, 119861.0)            # newer export, newest wins
    twin = datetime(2025, 4, 22, 15, 23)
    await _event(line, twin, 12, 8.274288)
    await _event(line, twin, 12, 1090807000.0)        # float bits as int, newer
    copies = datetime(2026, 9, 1, 10, 20, 45)
    for _ in range(3):
        await _event(line, copies, 259, 19.3)
    await _event(line, noise, 3, 5.0)                 # another type, same moment

    await _execute(sys_migration.STATEMENTS)

    async with async_session_factory() as session:
        rows = (await session.execute(text(
            "SELECT period, sys_type_id, volume FROM sys_archive "
            "ORDER BY period, sys_type_id"))).all()
    assert [(r.period, r.sys_type_id, r.volume) for r in rows] == [
        (twin, 12, pytest.approx(8.274288)),
        (copies, 259, pytest.approx(19.3)),
        (noise, 2, 119861.0),
        (noise, 3, 5.0),
    ]
