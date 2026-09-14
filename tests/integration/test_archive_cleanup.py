"""Removing archive rows a metering point should never have had.

Destructive and rare, and bounded twice: by the dates asked for, and by the
window each corrector actually stood at that point. The dates are commercial
days, which is the part worth pinning — gas day D runs from D 07:00 to D+1
07:00, so a purge that used calendar midnight would take seven hours of the
wrong day at each end and leave the daily row disagreeing with the hours under
it.
"""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import text

from backend.db.engine import async_session_factory
from backend.services import archive_cleanup

pytestmark = pytest.mark.asyncio


async def one_point(ser_num: int = 7001, installed_from=None, removed_at=None) -> dict:
    """A metering point with one corrector standing at it."""
    async with async_session_factory() as session:
        async with session.begin():
            device_id = (await session.execute(
                text("INSERT INTO dpd_device (ser_num, ch_num) "
                     "VALUES (:s, 0) RETURNING id"),
                {"s": ser_num},
            )).scalar_one()
            enterprise_id = (await session.execute(
                text("INSERT INTO enterprise (enterprise_name, active, enabled) "
                     "VALUES (:n, true, true) RETURNING id"),
                {"n": f"Точка {ser_num}"},
            )).scalar_one()
            await session.execute(
                text("INSERT INTO enterprise_device "
                     "(enterprise_id, device_id, installed_from, removed_at) "
                     "VALUES (:e, :d, :f, :r)"),
                {"e": enterprise_id, "d": device_id,
                 "f": installed_from or datetime(2020, 1, 1), "r": removed_at},
            )
    return {"enterprise_id": enterprise_id, "device_id": device_id}


async def fill_hours(device_id: int, first: datetime, count: int) -> None:
    async with async_session_factory() as session:
        async with session.begin():
            for i in range(count):
                await session.execute(
                    text("INSERT INTO dpd_hourly_archive "
                         "(device_id, stamp, dvst_alwrk, source) "
                         "VALUES (:d, :s, 1, 'dpd')"),
                    {"d": device_id, "s": first + timedelta(hours=i)},
                )


async def fill_days(device_id: int, first: date, count: int) -> None:
    async with async_session_factory() as session:
        async with session.begin():
            for i in range(count):
                await session.execute(
                    text("INSERT INTO dpd_daily_archive "
                         "(device_id, day, dvst_alwrk, source) "
                         "VALUES (:d, :day, 1, 'dpd')"),
                    {"d": device_id, "day": first + timedelta(days=i)},
                )


async def hours_left(device_id: int) -> list[datetime]:
    async with async_session_factory() as session:
        rows = await session.execute(
            text("SELECT stamp FROM dpd_hourly_archive WHERE device_id = :d "
                 "ORDER BY stamp"),
            {"d": device_id},
        )
        return [r[0] for r in rows]


async def days_left(device_id: int) -> list[date]:
    async with async_session_factory() as session:
        rows = await session.execute(
            text("SELECT day FROM dpd_daily_archive WHERE device_id = :d ORDER BY day"),
            {"d": device_id},
        )
        return [r[0] for r in rows]


class TestTheRangeIsInGasDays:
    async def test_an_hour_is_cleared_by_the_day_it_belongs_to(self):
        """06:00 on the 6th is still the 5th's gas day; 07:00 is the 6th's."""
        point = await one_point(7001)
        # Three whole gas days of hours: 05.06 07:00 through 08.06 06:00.
        await fill_hours(point["device_id"], datetime(2026, 6, 5, 7), 72)

        since, until = archive_cleanup.day_bounds(date(2026, 6, 5), date(2026, 6, 6))
        assert since == datetime(2026, 6, 5, 7)
        assert until == datetime(2026, 6, 7, 7)       # exclusive

        async with async_session_factory() as session:
            async with session.begin():
                removed = await archive_cleanup.purge(
                    session, point["enterprise_id"], since, until
                )

        assert removed["hourly"] == 48                # two gas days, not 48±7
        left = await hours_left(point["device_id"])
        assert left[0] == datetime(2026, 6, 7, 7)     # the third day, untouched
        assert left[-1] == datetime(2026, 6, 8, 6)

    async def test_daily_rows_go_by_the_day_they_close(self):
        point = await one_point(7002)
        await fill_days(point["device_id"], date(2026, 6, 3), 8)

        since, until = archive_cleanup.day_bounds(date(2026, 6, 5), date(2026, 6, 6))
        async with async_session_factory() as session:
            async with session.begin():
                removed = await archive_cleanup.purge(
                    session, point["enterprise_id"], since, until
                )

        assert removed["daily"] == 2
        assert date(2026, 6, 5) not in await days_left(point["device_id"])
        assert date(2026, 6, 7) in await days_left(point["device_id"])


class TestItStaysInsideTheHistory:
    async def test_rows_a_corrector_made_elsewhere_are_left_alone(self):
        """The archive is keyed by corrector, and correctors move.

        Taken off this point on the 6th, the hours it recorded afterwards
        belong to wherever it went — deleting "this point's archive" by device
        alone would take another point's gas with it.
        """
        point = await one_point(
            7003,
            installed_from=datetime(2026, 6, 5, 7),
            removed_at=datetime(2026, 6, 6, 7),
        )
        await fill_hours(point["device_id"], datetime(2026, 6, 5, 7), 72)

        since, until = archive_cleanup.day_bounds(date(2026, 6, 1), date(2026, 6, 30))
        async with async_session_factory() as session:
            async with session.begin():
                removed = await archive_cleanup.purge(
                    session, point["enterprise_id"], since, until
                )

        assert removed["hourly"] == 24                # only the day it stood here
        assert (await hours_left(point["device_id"]))[0] == datetime(2026, 6, 6, 7)

    async def test_a_preview_counts_and_removes_nothing(self):
        point = await one_point(7004)
        await fill_hours(point["device_id"], datetime(2026, 6, 5, 7), 24)
        await fill_days(point["device_id"], date(2026, 6, 5), 1)

        since, until = archive_cleanup.day_bounds(date(2026, 6, 5), date(2026, 6, 5))
        async with async_session_factory() as session:
            seen = await archive_cleanup.preview(
                session, point["enterprise_id"], since, until
            )

        assert (seen["hourly"], seen["daily"]) == (24, 1)
        assert len(await hours_left(point["device_id"])) == 24
        assert seen["devices"][0]["ser_num"] == 7004
