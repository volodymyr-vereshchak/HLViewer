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
    _PAYLOAD_FIELDS,
    fetch_dpd_volumes,
)

BRANCH_ID = 1

# Long-closed gas days (final rows, CACHE_TTL applies).
DAY1, DAY2, DAY3 = date(2024, 12, 20), date(2024, 12, 21), date(2024, 12, 22)


def make_device(ser_num: int) -> dict:
    return {
        "serNum": ser_num, "mfDev": 1, "typeDev": 3, "chNum": 0,
        "branch_id": BRANCH_ID, "line_id": 1, "enterprise_name": f"ent-{ser_num}",
    }


def device_key(device: dict) -> tuple:
    return (device["serNum"], device["mfDev"], device["typeDev"], device["chNum"])


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


async def set_fetched_at(ts: datetime) -> None:
    async with async_session_factory() as session:
        await session.execute(
            text("UPDATE dpd_volume_cache SET fetched_at = :ts"), {"ts": ts}
        )
        await session.commit()


async def max_fetched_at() -> datetime:
    async with async_session_factory() as session:
        return (
            await session.execute(select(func.max(DpdVolumeCache.fetched_at)))
        ).scalar_one()


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

    async def test_open_day_cached_but_repolled_every_request(self, dpd_mock):
        """The still-open gas day is stored like everything else, but stays a
        "gap": every request re-polls it and overwrites the snapshot. The
        snapshot serves as a fallback when the poll brings nothing back."""
        today = date.today()  # gas day closes tomorrow 07:00 → open now
        devices = [make_device(101)]
        dpd_mock.get_volumes.return_value = daily_records(devices, [today])

        first = await fetch_dpd_volumes(devices, as_dt(today), as_dt(today), "daily")
        assert record_keys(first) == {(101, today.isoformat())}
        assert await cache_row_count() == 1  # snapshot stored

        second = await fetch_dpd_volumes(devices, as_dt(today), as_dt(today), "daily")
        assert dpd_mock.get_volumes.await_count == 2  # unfinished day → re-polled
        assert record_keys(second) == record_keys(first)

        # Poll came back empty (device hiccup) → the snapshot still serves.
        dpd_mock.get_volumes.return_value = []
        third = await fetch_dpd_volumes(devices, as_dt(today), as_dt(today), "daily")
        assert record_keys(third) == record_keys(first)

    async def test_mixed_range_serves_final_days_repolls_open(self, dpd_mock):
        """A range spanning closed days and today: the closed days are served
        from cache untouched, only the open remainder is re-polled."""
        today = date.today()
        week_ago = today - timedelta(days=7)  # closed long ago
        devices = [make_device(101)]
        dpd_mock.get_volumes.return_value = daily_records(devices, [week_ago, today])

        first = await fetch_dpd_volumes(devices, as_dt(week_ago), as_dt(today), "daily")
        assert record_keys(first) == {(101, week_ago.isoformat()), (101, today.isoformat())}
        assert await cache_row_count() == 2  # final day + today's snapshot

        dpd_mock.get_volumes.reset_mock()
        dpd_mock.get_volumes.return_value = daily_records(devices, [today])

        second = await fetch_dpd_volumes(devices, as_dt(week_ago), as_dt(today), "daily")

        dpd_mock.get_volumes.assert_awaited_once()
        _, poll_from, poll_to = dpd_mock.get_volumes.await_args.args
        # week_ago is served from cache; the empty middle days + today re-asked.
        assert (poll_from.date(), poll_to.date()) == (week_ago + timedelta(days=1), today)
        assert record_keys(second) == record_keys(first)

    async def test_snapshot_repolled_after_day_closes_then_final(self, dpd_mock):
        """A row fetched while its day was open is incomplete. Once the day
        closes it must be re-polled despite being only days old; the re-poll
        makes it final and it is never asked again."""
        day = date.today() - timedelta(days=7)  # closed long ago
        devices = [make_device(101)]
        dpd_mock.get_volumes.return_value = daily_records(devices, [day])

        await fetch_dpd_volumes(devices, as_dt(day), as_dt(day), "daily")
        # Pretend the poll happened at 18:00 of the day itself (still open).
        await set_fetched_at(datetime.combine(day, datetime.min.time()) + timedelta(hours=18))
        dpd_mock.get_volumes.reset_mock()

        await fetch_dpd_volumes(devices, as_dt(day), as_dt(day), "daily")
        dpd_mock.get_volumes.assert_awaited_once()  # snapshot not trusted

        dpd_mock.get_volumes.reset_mock()
        await fetch_dpd_volumes(devices, as_dt(day), as_dt(day), "daily")
        dpd_mock.get_volumes.assert_not_awaited()  # final now

    async def test_untouched_rows_pruned_after_retention(self, dpd_mock):
        devices = [make_device(101)]
        dpd_mock.get_volumes.return_value = daily_records(devices, [DAY1, DAY2])

        await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY2), "daily")
        await shift_fetched_at(timedelta(days=8))  # not viewed for over a week
        dpd_mock.get_volumes.return_value = daily_records(devices, [DAY3])

        # Any poll prunes expired rows in passing.
        await fetch_dpd_volumes(devices, as_dt(DAY3), as_dt(DAY3), "daily")

        assert await cache_row_count() == 1  # DAY1/DAY2 gone, DAY3 cached

    async def test_read_extends_ttl_sliding(self, dpd_mock):
        """Reading final rows refreshes fetched_at: data viewed at least once
        a week is never re-polled."""
        devices = [make_device(101)]
        dpd_mock.get_volumes.return_value = daily_records(devices, [DAY1, DAY2])

        await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY2), "daily")
        dpd_mock.get_volumes.reset_mock()

        await shift_fetched_at(timedelta(days=6, hours=23))  # nearly expired
        before = datetime.now()
        await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY2), "daily")

        dpd_mock.get_volumes.assert_not_awaited()
        assert await max_fetched_at() >= before  # the read touched the rows

        # Without the touch this would be ~14 days old and re-polled.
        await shift_fetched_at(timedelta(days=6, hours=23))
        await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY2), "daily")
        dpd_mock.get_volumes.assert_not_awaited()

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

    async def test_per_device_spans(self, dpd_mock):
        """A device with no cached data must not force devices that miss a
        single day to re-download the whole range."""
        dev_a, dev_b = make_device(101), make_device(102)
        # A gets DAY1..DAY2 cached (DAY3 empty at DPD).
        dpd_mock.get_volumes.return_value = daily_records([dev_a], [DAY1, DAY2])
        await fetch_dpd_volumes([dev_a], as_dt(DAY1), as_dt(DAY3), "daily")

        dpd_mock.get_volumes.reset_mock()
        dpd_mock.get_volumes.return_value = daily_records([dev_b], [DAY1, DAY2, DAY3])

        await fetch_dpd_volumes([dev_a, dev_b], as_dt(DAY1), as_dt(DAY3), "daily")

        dpd_mock.get_volumes.assert_awaited_once()
        polled, poll_from, poll_to = dpd_mock.get_volumes.await_args.args
        assert polled == [dev_a, dev_b]
        assert (poll_from.date(), poll_to.date()) == (DAY1, DAY3)  # envelope
        ranges = dpd_mock.get_volumes.await_args.kwargs["device_ranges"]
        assert ranges[device_key(dev_a)] == (as_dt(DAY3), as_dt(DAY3))
        assert ranges[device_key(dev_b)] == (as_dt(DAY1), as_dt(DAY3))

    async def test_merge_no_duplicates_outside_span(self, dpd_mock):
        """Fresh records past a device's polled span (the hourly `to`+1
        widening tail) must not duplicate days already served from cache,
        and must never be cached themselves."""
        dev = make_device(101)
        # DAY2 empty at DPD → cached rows only for DAY1 and DAY3.
        dpd_mock.get_volumes.return_value = daily_records([dev], [DAY1, DAY3])
        await fetch_dpd_volumes([dev], as_dt(DAY1), as_dt(DAY3), "daily")
        assert await cache_row_count() == 2

        dpd_mock.get_volumes.reset_mock()
        # Re-poll of the DAY2 hole also returns a stray DAY3 record (tail).
        dpd_mock.get_volumes.return_value = daily_records([dev], [DAY2, DAY3])

        records = await fetch_dpd_volumes([dev], as_dt(DAY1), as_dt(DAY3), "daily")

        _, poll_from, poll_to = dpd_mock.get_volumes.await_args.args
        assert (poll_from.date(), poll_to.date()) == (DAY2, DAY2)
        assert record_keys(records) == {(101, d.isoformat()) for d in (DAY1, DAY2, DAY3)}
        assert len(records) == 3  # DAY3 served once (from cache), no dup
        assert await cache_row_count() == 3  # DAY2 added; stray DAY3 not cached

    async def test_payload_whitelisted_and_ids_restored(self, dpd_mock):
        dev = make_device(101)
        recs = daily_records([dev], [DAY1])
        recs[0].update({"korr": 0.98, "status": 7})  # ballast DPD fields
        dpd_mock.get_volumes.return_value = recs

        await fetch_dpd_volumes([dev], as_dt(DAY1), as_dt(DAY1), "daily")

        async with async_session_factory() as session:
            row = (await session.execute(select(DpdVolumeCache))).scalars().one()
        assert set(row.payload[0]) <= set(_PAYLOAD_FIELDS)
        assert "serNum" not in row.payload[0]

        dpd_mock.get_volumes.reset_mock()
        cached = await fetch_dpd_volumes([dev], as_dt(DAY1), as_dt(DAY1), "daily")

        dpd_mock.get_volumes.assert_not_awaited()
        assert cached[0]["serNum"] == 101  # identifiers restored from the row key
        assert cached[0]["press"] == 101.3
        assert "korr" not in cached[0]

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

        async def slow_get_volumes(polled, date_from, date_to, **kwargs):
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
