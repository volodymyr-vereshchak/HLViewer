"""Migration 331122169b34 on rows stored before the DAOs marked anything.

The test schema is built from the models and already has `volume_fix`; the
migration's own statements are run on rows planted the way the old code
stored them — negative and unmarked.
"""
import importlib.util
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from backend.db.engine import async_session_factory

MIGRATION = (
    Path(__file__).parents[2]
    / "backend/db/alembic/versions/331122169b34_dpd_negative_volume_to_zero.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("set_back_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load()
FULL_DAY, SHORT_DAY = date(2026, 3, 10), date(2026, 3, 11)


async def _execute(statements, params=None):
    async with async_session_factory() as session:
        for statement in statements:
            await session.execute(text(statement), params or {})
        await session.commit()


async def _migrate():
    for daily, hourly, key, columns, _index in migration.ARCHIVES:
        await _execute([
            *migration.mark_statements(daily, columns),
            *migration.mark_statements(hourly, columns),
            migration.sum_statement(daily, hourly, key, columns, 7, "Europe/Kyiv"),
        ])


async def test_old_negatives_are_marked_and_complete_days_summed(clean_db):
    async with async_session_factory() as session:
        device = (await session.execute(text(
            "INSERT INTO dpd_device (ser_num, mf_dev, type_dev, ch_num) "
            "VALUES (9001, 1, 3, 0) RETURNING id"
        ))).scalar_one()
        await session.commit()

    start = datetime.combine(FULL_DAY, datetime.min.time()) + timedelta(hours=7)
    hours = [start + timedelta(hours=i) for i in range(24)]
    async with async_session_factory() as session:
        for h in hours:
            # The hour of the jump came in negative too.
            await session.execute(text(
                "INSERT INTO dpd_hourly_archive (device_id, stamp, dvst_alwrk) "
                "VALUES (:d, :s, :v)"
            ), {"d": device, "s": h, "v": -50.0 if h == hours[3] else 2.0})
        for day, volume, work in ((FULL_DAY, -1771792.28, None), (SHORT_DAY, -10.0, 4.0)):
            await session.execute(text(
                "INSERT INTO dpd_daily_archive (device_id, day, dvst_alwrk, dvwrk_alwrk) "
                "VALUES (:d, :day, :v, :w)"
            ), {"d": device, "day": day, "v": volume, "w": work})
        await session.commit()

    await _migrate()

    async with async_session_factory() as session:
        days = (await session.execute(text(
            "SELECT day, dvst_alwrk, dvwrk_alwrk, volume_fix FROM dpd_daily_archive ORDER BY day"
        ))).all()
        jump = (await session.execute(text(
            "SELECT dvst_alwrk, volume_fix FROM dpd_hourly_archive WHERE stamp = :s"
        ), {"s": hours[3]})).one()
    # 23 hours × 2.0, the jump hour 0; no hourly dvwrk, so its sum is NULL too.
    assert tuple(days[0]) == (FULL_DAY, 46.0, None, "hours")
    # No hours for this day: 0 and marked; the positive column untouched.
    assert tuple(days[1]) == (SHORT_DAY, 0.0, 4.0, "zeroed")
    assert tuple(jump) == (0.0, "zeroed")
