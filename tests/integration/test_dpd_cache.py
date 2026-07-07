"""Integration tests for the Postgres DPD cache + per-branch advisory-lock
dedup in enterprise_volume_service.fetch_dpd_volumes.

DPDClient.for_branch is mocked (no live DPD), everything else — real test
Postgres. Freshness windows are exercised by rewriting fetched_at directly."""

import asyncio
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, text
from sqlmodel import select

from backend.db.engine import async_session_factory
from backend.db.models.dpd_cache_model import DpdVolumeCache
from backend.services.enterprise_volume_service import (
    _gas_today,
    fetch_dpd_volumes,
)

BRANCH_ID = 1

# Long-closed gas days (CLOSED_DAY_TTL applies).
DAY1, DAY2, DAY3 = date(2024, 12, 20), date(2024, 12, 21), date(2024, 12, 22)


def make_device(ser_num: int) -> dict:
    return {
        "serNum": ser_num, "mfDev": 1, "typeDev": 3, "chNum": 0,
        "branch_id": BRANCH_ID, "line_id": 1, "enterprise_name": f"ent-{ser_num}",
    }


def daily_records(devices: list[dict], days: list[date]) -> list[dict]:
    return [
        {
            "serNum": d["serNum"], "mfDev": d["mfDev"], "typeDev": d["typeDev"],
            "chNum": d["chNum"], "date": day.isoformat(), "dvstAlwrk": 10.0,
            "press": 101.3, "temper": 15.0,
        }
        for d in devices
        for day in days
    ]


def record_keys(records: list[dict]) -> set[tuple]:
    return {(r["serNum"], r["date"]) for r in records}


def as_dt(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time())


async def cache_row_count() -> int:
    async with async_session_factory() as session:
        return (
            await session.execute(
                select(func.count()).select_from(DpdVolumeCache)
            )
        ).scalar_one()


async def shift_fetched_at(delta: timedelta) -> None:
    async with async_session_factory() as session:
        await session.execute(
            text(
                "UPDATE dpd_volume_cache "
                "SET fetched_at = fetched_at - CAST(:d AS interval)"
            ),
            {"d": delta},
        )
        await session.commit()


@pytest.fixture
def dpd_mock(mocker, clean_db):
    """Patched DPDClient.for_branch returning a client with AsyncMock
    get_volumes. Tests set .return_value / .side_effect as needed."""
    client = mocker.AsyncMock()
    mocker.patch(
        "backend.services.enterprise_volume_service.DPDClient.for_branch",
        mocker.AsyncMock(return_value=client),
    )
    return client


class TestDpdCache:
    async def test_first_call_polls_everything_and_caches(self, dpd_mock):
        devices = [make_device(101), make_device(102)]
        dpd_mock.get_volumes.return_value = daily_records(devices, [DAY1, DAY2, DAY3])

        records = await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY3), "daily")

        dpd_mock.get_volumes.assert_awaited_once()
        polled_devices, poll_from, poll_to = dpd_mock.get_volumes.await_args.args
        assert polled_devices == devices
        assert (poll_from.date(), poll_to.date()) == (DAY1, DAY3)
        assert len(records) == 6
        assert await cache_row_count() == 6  # 2 devices × 3 days

    async def test_second_call_within_ttl_serves_from_cache(self, dpd_mock):
        devices = [make_device(101), make_device(102)]
        dpd_mock.get_volumes.return_value = daily_records(devices, [DAY1, DAY2, DAY3])

        first = await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY3), "daily")
        dpd_mock.get_volumes.reset_mock()

        second = await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY3), "daily")

        dpd_mock.get_volumes.assert_not_awaited()
        assert record_keys(second) == record_keys(first)

    async def test_current_gas_day_repolled_after_short_ttl(self, dpd_mock):
        today = _gas_today(datetime.now())
        days = [today - timedelta(days=2), today - timedelta(days=1), today]
        devices = [make_device(101)]
        dpd_mock.get_volumes.return_value = daily_records(devices, days)

        await fetch_dpd_volumes(devices, as_dt(days[0]), as_dt(today), "daily")
        # Past the 5-minute current-day window, well inside the 24h closed-day one.
        await shift_fetched_at(timedelta(minutes=10))
        dpd_mock.get_volumes.reset_mock()
        dpd_mock.get_volumes.return_value = daily_records(devices, [today])

        records = await fetch_dpd_volumes(devices, as_dt(days[0]), as_dt(today), "daily")

        dpd_mock.get_volumes.assert_awaited_once()
        _, poll_from, poll_to = dpd_mock.get_volumes.await_args.args
        assert (poll_from.date(), poll_to.date()) == (today, today)
        # Merged result still covers the whole range: 2 cached days + fresh today.
        assert record_keys(records) == {(101, d.isoformat()) for d in days}

    async def test_stale_closed_days_repolled(self, dpd_mock):
        devices = [make_device(101)]
        dpd_mock.get_volumes.return_value = daily_records(devices, [DAY1, DAY2])

        await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY2), "daily")
        await shift_fetched_at(timedelta(hours=25))
        dpd_mock.get_volumes.reset_mock()

        await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY2), "daily")

        dpd_mock.get_volumes.assert_awaited_once()
        _, poll_from, poll_to = dpd_mock.get_volumes.await_args.args
        assert (poll_from.date(), poll_to.date()) == (DAY1, DAY2)

    async def test_empty_days_not_cached_and_repolled(self, dpd_mock):
        devices = [make_device(101)]
        # DPD has data only for DAY1 and DAY2; DAY3 comes back empty.
        dpd_mock.get_volumes.return_value = daily_records(devices, [DAY1, DAY2])

        first = await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY3), "daily")
        assert await cache_row_count() == 2  # no row for the empty DAY3

        dpd_mock.get_volumes.reset_mock()
        dpd_mock.get_volumes.return_value = []

        second = await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY3), "daily")

        # Only the absent day is re-asked, cached days are not.
        dpd_mock.get_volumes.assert_awaited_once()
        _, poll_from, poll_to = dpd_mock.get_volumes.await_args.args
        assert (poll_from.date(), poll_to.date()) == (DAY3, DAY3)
        assert record_keys(second) == record_keys(first)

    async def test_hourly_and_daily_cached_independently(self, dpd_mock):
        devices = [make_device(101)]
        dpd_mock.get_volumes.return_value = [
            {"serNum": 101, "mfDev": 1, "typeDev": 3, "chNum": 0,
             "date": f"{DAY1.isoformat()}T{h:02d}:00:00", "dvstAlwrk": 1.0}
            for h in range(24)
        ]

        hourly = await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY1), "hourly")
        assert len(hourly) == 24
        assert await cache_row_count() == 1

        dpd_mock.get_volumes.reset_mock()
        dpd_mock.get_volumes.return_value = daily_records(devices, [DAY1])

        # Same day, other period_type → its own cache entry, so DPD is polled.
        await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY1), "daily")
        dpd_mock.get_volumes.assert_awaited_once()
        assert await cache_row_count() == 2

        # And the hourly entry still serves without a poll.
        dpd_mock.get_volumes.reset_mock()
        again = await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY1), "hourly")
        dpd_mock.get_volumes.assert_not_awaited()
        assert len(again) == 24


class TestDpdDedup:
    async def test_concurrent_identical_requests_poll_once(self, dpd_mock):
        devices = [make_device(101), make_device(102)]
        first_poll_started = asyncio.Event()
        release_poll = asyncio.Event()
        calls = {"count": 0}

        async def slow_get_volumes(polled, date_from, date_to, type_request):
            calls["count"] += 1
            first_poll_started.set()
            await release_poll.wait()
            return daily_records(polled, [DAY1, DAY2])

        dpd_mock.get_volumes = slow_get_volumes

        leader = asyncio.create_task(
            fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY2), "daily")
        )
        await first_poll_started.wait()
        follower = asyncio.create_task(
            fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY2), "daily")
        )
        # Let the follower reach and block on the branch advisory lock.
        await asyncio.sleep(0.3)
        release_poll.set()

        first, second = await asyncio.gather(leader, follower)

        assert calls["count"] == 1  # the follower was served from cache
        assert record_keys(first) == record_keys(second)
