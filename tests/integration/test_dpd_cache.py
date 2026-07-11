"""Integration tests for the Postgres DPD cache + per-branch advisory-lock
dedup in enterprise_volume_service.fetch_dpd_volumes.

DPDClient.for_branch is mocked (no live DPD), everything else — real test
Postgres. Cache model under test: the gap unit is the record stamp (hour for
hourly, day for daily), everything a poll returns is final and cached, only
missing stamps are re-polled, fetched_at drives 7-day sliding retention only.
For hourly a bare date range means commercial days: from=D1&to=D2 is the
stamp window [D1 07:00 .. D2+1 06:00]."""

import asyncio
import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, text
from sqlmodel import select

from backend.api.endpoints.enterprise_ep import EnterpriseRouter
from backend.db.engine import async_session_factory
from backend.db.models.dpd_cache_model import DpdVolumeCache
from backend.services.enterprise_volume_service import (
    _PAYLOAD_FIELDS,
    fetch_dpd_volumes,
)

BRANCH_ID = 1

DAY1, DAY2, DAY3 = date(2024, 12, 20), date(2024, 12, 21), date(2024, 12, 22)
# Inside HOLE_RETRY_WINDOW: holes on these days are still re-polled.
YESTERDAY = date.today() - timedelta(days=1)
TODAY = date.today()


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


def commercial_stamps(day: date, hours=range(24)) -> list[datetime]:
    """Hour stamps of the commercial day: offset 0 = D 07:00 ... 23 = D+1 06:00."""
    base = datetime.combine(day, datetime.min.time()) + timedelta(hours=7)
    return [base + timedelta(hours=i) for i in hours]


def hourly_records(devices: list[dict], stamps: list[datetime]) -> list[dict]:
    return [
        {
            "serNum": d["serNum"], "mfDev": d["mfDev"], "typeDev": d["typeDev"],
            "chNum": d["chNum"], "date": s.strftime("%Y-%m-%dT%H:%M:%S"),
            "dvstAlwrk": 1.0,
        }
        for d in devices
        for s in stamps
    ]


def record_keys(records: list[dict]) -> set[tuple]:
    return {(r["serNum"], r["date"]) for r in records}


async def insert_cache_row(device: dict, day: date, payload: list[dict],
                           period_type: str = "hourly") -> None:
    """Plant a pre-existing cache row (e.g. what older code versions wrote)."""
    async with async_session_factory() as session:
        session.add(DpdVolumeCache(
            ser_num=device["serNum"], mf_dev=device["mfDev"],
            type_dev=device["typeDev"], ch_num=device["chNum"],
            period_type=period_type, day=day, payload=payload,
            fetched_at=datetime.now(),
        ))
        await session.commit()


def as_dt(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time())


async def cache_row_count() -> int:
    async with async_session_factory() as session:
        return (
            await session.execute(
                select(func.count()).select_from(DpdVolumeCache)
            )
        ).scalar_one()


async def cache_rows() -> list[DpdVolumeCache]:
    async with async_session_factory() as session:
        return list((await session.execute(select(DpdVolumeCache))).scalars().all())


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


class TestDpdCacheDaily:
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

    async def test_second_call_serves_from_cache(self, dpd_mock):
        devices = [make_device(101), make_device(102)]
        dpd_mock.get_volumes.return_value = daily_records(devices, [DAY1, DAY2, DAY3])

        first = await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY3), "daily")
        dpd_mock.get_volumes.reset_mock()

        second = await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY3), "daily")

        dpd_mock.get_volumes.assert_not_awaited()
        assert record_keys(second) == record_keys(first)

    async def test_returned_data_final_immediately(self, dpd_mock):
        """Whatever DPD returns is final the moment it arrives — even today's
        record: once cached it is never re-asked."""
        today = date.today()
        devices = [make_device(101)]
        dpd_mock.get_volumes.return_value = daily_records(devices, [today])

        first = await fetch_dpd_volumes(devices, as_dt(today), as_dt(today), "daily")
        assert record_keys(first) == {(101, today.isoformat())}
        assert await cache_row_count() == 1

        dpd_mock.get_volumes.reset_mock()
        second = await fetch_dpd_volumes(devices, as_dt(today), as_dt(today), "daily")
        dpd_mock.get_volumes.assert_not_awaited()
        assert record_keys(second) == record_keys(first)

    async def test_old_holes_become_permanent_after_first_try(self, dpd_mock):
        """A hole older than HOLE_RETRY_WINDOW whose day was already polled is
        permanent: the data will never appear at DPD, so it is not re-asked
        (this was the amplification that made every poll re-download weeks)."""
        devices = [make_device(101)]
        dpd_mock.get_volumes.return_value = daily_records(devices, [DAY1, DAY3])

        first = await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY3), "daily")
        # DAY2 returned nothing but was tried → empty marker row.
        assert await cache_row_count() == 3

        dpd_mock.get_volumes.reset_mock()
        second = await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY3), "daily")

        dpd_mock.get_volumes.assert_not_awaited()  # DAY2 hole is permanent
        assert record_keys(second) == record_keys(first)

    async def test_recent_holes_still_repolled(self, dpd_mock):
        """Holes inside HOLE_RETRY_WINDOW keep being re-asked — late-arriving
        telemetry can still fill them."""
        day_before = TODAY - timedelta(days=2)
        devices = [make_device(101)]
        dpd_mock.get_volumes.return_value = daily_records(devices, [day_before])

        await fetch_dpd_volumes(
            devices, as_dt(day_before), as_dt(YESTERDAY), "daily"
        )
        assert await cache_row_count() == 2  # data + tried-mark for YESTERDAY

        dpd_mock.get_volumes.reset_mock()
        dpd_mock.get_volumes.return_value = daily_records(devices, [YESTERDAY])

        second = await fetch_dpd_volumes(
            devices, as_dt(day_before), as_dt(YESTERDAY), "daily"
        )

        dpd_mock.get_volumes.assert_awaited_once()
        _, poll_from, poll_to = dpd_mock.get_volumes.await_args.args
        assert (poll_from.date(), poll_to.date()) == (YESTERDAY, YESTERDAY)
        assert record_keys(second) == {
            (101, day_before.isoformat()), (101, YESTERDAY.isoformat()),
        }

    async def test_per_device_spans(self, dpd_mock):
        """A device with no cached data must not force devices that miss a
        single day to re-download the whole range."""
        dev_a, dev_b = make_device(101), make_device(102)
        # A gets two days cached; YESTERDAY stays a (recent) hole.
        day_before = TODAY - timedelta(days=2)
        day_first = TODAY - timedelta(days=3)
        dpd_mock.get_volumes.return_value = daily_records(
            [dev_a], [day_first, day_before]
        )
        await fetch_dpd_volumes([dev_a], as_dt(day_first), as_dt(YESTERDAY), "daily")

        dpd_mock.get_volumes.reset_mock()
        dpd_mock.get_volumes.return_value = daily_records(
            [dev_b], [day_first, day_before, YESTERDAY]
        )

        await fetch_dpd_volumes(
            [dev_a, dev_b], as_dt(day_first), as_dt(YESTERDAY), "daily"
        )

        dpd_mock.get_volumes.assert_awaited_once()
        polled, poll_from, poll_to = dpd_mock.get_volumes.await_args.args
        assert polled == [dev_a, dev_b]
        assert (poll_from.date(), poll_to.date()) == (day_first, YESTERDAY)
        ranges = dpd_mock.get_volumes.await_args.kwargs["device_ranges"]
        assert ranges[device_key(dev_a)] == (as_dt(YESTERDAY), as_dt(YESTERDAY))
        assert ranges[device_key(dev_b)] == (as_dt(day_first), as_dt(YESTERDAY))

    async def test_out_of_window_records_cached_not_returned(self, dpd_mock):
        """Records past the requested window (the commercial-date rounding
        tail) are final data: they are cached for later but clipped from the
        response, and never duplicate cached days."""
        dev = make_device(101)
        dpd_mock.get_volumes.return_value = daily_records([dev], [DAY1])
        await fetch_dpd_volumes([dev], as_dt(DAY1), as_dt(DAY1), "daily")

        dpd_mock.get_volumes.reset_mock()
        # Re-poll of the DAY2 hole also returns a stray DAY3 record.
        dpd_mock.get_volumes.return_value = daily_records([dev], [DAY2, DAY3])

        records = await fetch_dpd_volumes([dev], as_dt(DAY1), as_dt(DAY2), "daily")

        _, poll_from, poll_to = dpd_mock.get_volumes.await_args.args
        assert (poll_from.date(), poll_to.date()) == (DAY2, DAY2)
        assert record_keys(records) == {(101, d.isoformat()) for d in (DAY1, DAY2)}
        assert await cache_row_count() == 3  # stray DAY3 cached for later

        # ...so a wider request later finds DAY3 already cached.
        dpd_mock.get_volumes.reset_mock()
        wider = await fetch_dpd_volumes([dev], as_dt(DAY1), as_dt(DAY3), "daily")
        dpd_mock.get_volumes.assert_not_awaited()
        assert record_keys(wider) == {(101, d.isoformat()) for d in (DAY1, DAY2, DAY3)}

    async def test_untouched_rows_pruned_after_retention(self, dpd_mock):
        devices = [make_device(101)]
        dpd_mock.get_volumes.return_value = daily_records(devices, [DAY1, DAY2])

        await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY2), "daily")
        await shift_fetched_at(timedelta(days=8))  # not viewed for over a week
        dpd_mock.get_volumes.return_value = daily_records(devices, [DAY3])

        # Any poll prunes expired rows in passing.
        await fetch_dpd_volumes(devices, as_dt(DAY3), as_dt(DAY3), "daily")

        assert await cache_row_count() == 1  # DAY1/DAY2 gone, DAY3 cached

    async def test_read_extends_retention_sliding(self, dpd_mock):
        """Reading rows refreshes fetched_at: data viewed at least once a week
        is never re-polled."""
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

    async def test_payload_whitelisted_and_ids_restored(self, dpd_mock):
        dev = make_device(101)
        recs = daily_records([dev], [DAY1])
        recs[0].update({"korr": 0.98, "status": 7})  # ballast DPD fields
        dpd_mock.get_volumes.return_value = recs

        await fetch_dpd_volumes([dev], as_dt(DAY1), as_dt(DAY1), "daily")

        rows = await cache_rows()
        assert set(rows[0].payload[0]) <= set(_PAYLOAD_FIELDS)
        assert "serNum" not in rows[0].payload[0]

        dpd_mock.get_volumes.reset_mock()
        cached = await fetch_dpd_volumes([dev], as_dt(DAY1), as_dt(DAY1), "daily")

        dpd_mock.get_volumes.assert_not_awaited()
        assert cached[0]["serNum"] == 101  # identifiers restored from the row key
        assert cached[0]["press"] == 101.3
        assert "korr" not in cached[0]


class TestDpdCacheHourly:
    async def test_commercial_window(self, dpd_mock):
        """from=D1&to=D1 hourly means the commercial day [D1 07:00..D2 06:00]:
        the response holds exactly those 24 stamps, none cut at midnight."""
        devices = [make_device(101)]
        stamps = commercial_stamps(DAY1)
        dpd_mock.get_volumes.return_value = hourly_records(devices, stamps)

        records = await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY1), "hourly")

        _, poll_from, poll_to = dpd_mock.get_volumes.await_args.args
        assert poll_from == datetime(2024, 12, 20, 7)
        assert poll_to == datetime(2024, 12, 21, 6)
        assert record_keys(records) == record_keys(hourly_records(devices, stamps))
        # Stamps live on two calendar days → two cache rows.
        assert await cache_row_count() == 2

        dpd_mock.get_volumes.reset_mock()
        again = await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY1), "hourly")
        dpd_mock.get_volumes.assert_not_awaited()
        assert len(again) == 24

    async def test_hole_repolled_from_first_missing_stamp(self, dpd_mock):
        """A device missing a few recent hours re-polls exactly the missing
        span, and the merge keeps the hours already cached."""
        devices = [make_device(101)]
        # First poll: only hours 07:00–16:00 published yet.
        dpd_mock.get_volumes.return_value = hourly_records(
            devices, commercial_stamps(YESTERDAY, range(10))
        )
        first = await fetch_dpd_volumes(
            devices, as_dt(YESTERDAY), as_dt(YESTERDAY), "hourly"
        )
        assert len(first) == 10
        # 10 stamps on YESTERDAY + an empty tried-mark row for TODAY's tail.
        assert await cache_row_count() == 2

        # Second request: the rest of the commercial day has appeared.
        dpd_mock.get_volumes.reset_mock()
        dpd_mock.get_volumes.return_value = hourly_records(
            devices, commercial_stamps(YESTERDAY, range(10, 24))
        )
        second = await fetch_dpd_volumes(
            devices, as_dt(YESTERDAY), as_dt(YESTERDAY), "hourly"
        )

        ranges = dpd_mock.get_volumes.await_args.kwargs["device_ranges"]
        assert ranges[device_key(devices[0])] == (
            as_dt(YESTERDAY) + timedelta(hours=17),  # first missing stamp
            as_dt(TODAY) + timedelta(hours=6),       # window end
        )
        assert len(second) == 24

        # Merge preserved the earlier hours in YESTERDAY's row.
        by_day = {row.day: row.payload for row in await cache_rows()}
        assert len(by_day[YESTERDAY]) == 17  # 07:00..23:00
        assert len(by_day[TODAY]) == 7  # 00:00..06:00

        # Fully cached now → no poll.
        dpd_mock.get_volumes.reset_mock()
        third = await fetch_dpd_volumes(
            devices, as_dt(YESTERDAY), as_dt(YESTERDAY), "hourly"
        )
        dpd_mock.get_volumes.assert_not_awaited()
        assert len(third) == 24

    async def test_out_of_window_stamps_cached_not_returned(self, dpd_mock):
        devices = [make_device(101)]
        stray = datetime(2024, 12, 20, 3)  # before the commercial window
        dpd_mock.get_volumes.return_value = hourly_records(
            devices, [stray] + commercial_stamps(DAY1)
        )

        records = await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY1), "hourly")

        assert len(records) == 24
        assert (101, stray.strftime("%Y-%m-%dT%H:%M:%S")) not in record_keys(records)
        by_day = {row.day: row.payload for row in await cache_rows()}
        assert len(by_day[DAY1]) == 18  # 03:00 stray merged alongside 07:00..23:00

    async def test_null_skeleton_records_not_cached_and_repolled(self, dpd_mock):
        """DPD returns a full commercial-day skeleton with null volumes for
        hours it has no data for yet. Those records are gaps, not data: they
        are returned to the caller but never cached, and the stamps stay
        missing until real values appear."""
        devices = [make_device(101)]
        skeleton = hourly_records(devices, commercial_stamps(YESTERDAY))
        for i, rec in enumerate(skeleton):
            if i not in (1, 2):  # data exists only for 08:00 and 09:00
                rec["dvstAlwrk"] = None

        dpd_mock.get_volumes.return_value = skeleton
        first = await fetch_dpd_volumes(
            devices, as_dt(YESTERDAY), as_dt(YESTERDAY), "hourly"
        )
        assert len(first) == 24  # nulls still returned to the caller

        by_day = {row.day: row.payload for row in await cache_rows()}
        assert len(by_day[YESTERDAY]) == 2  # only real data cached
        assert by_day[TODAY] == []  # tried-mark for the empty tail

        # Real values appeared at DPD → the null stamps are re-asked in full.
        dpd_mock.get_volumes.reset_mock()
        dpd_mock.get_volumes.return_value = hourly_records(
            devices, commercial_stamps(YESTERDAY)
        )
        second = await fetch_dpd_volumes(
            devices, as_dt(YESTERDAY), as_dt(YESTERDAY), "hourly"
        )

        ranges = dpd_mock.get_volumes.await_args.kwargs["device_ranges"]
        assert ranges[device_key(devices[0])] == (
            as_dt(YESTERDAY) + timedelta(hours=7),
            as_dt(TODAY) + timedelta(hours=6),
        )
        assert len(second) == 24
        assert all(r["dvstAlwrk"] is not None for r in second)

        dpd_mock.get_volumes.reset_mock()
        third = await fetch_dpd_volumes(
            devices, as_dt(YESTERDAY), as_dt(YESTERDAY), "hourly"
        )
        dpd_mock.get_volumes.assert_not_awaited()
        assert len(third) == 24

    async def test_stale_null_rows_in_db_treated_as_gaps(self, dpd_mock):
        """Rows written by older code may hold null-volume records; on read
        they must not satisfy their stamps — the holes get re-polled and the
        rewritten rows keep only real data."""
        dev = make_device(101)
        stamps = commercial_stamps(YESTERDAY)
        await insert_cache_row(dev, YESTERDAY, [
            {"date": s.strftime("%Y-%m-%dT%H:%M:%S"),
             "dvstAlwrk": 1.0 if i in (1, 2) else None}
            for i, s in enumerate(stamps) if s.date() == YESTERDAY
        ])
        await insert_cache_row(dev, TODAY, [
            {"date": s.strftime("%Y-%m-%dT%H:%M:%S"), "dvstAlwrk": None}
            for s in stamps if s.date() == TODAY
        ])

        dpd_mock.get_volumes.return_value = hourly_records([dev], stamps)
        records = await fetch_dpd_volumes(
            [dev], as_dt(YESTERDAY), as_dt(YESTERDAY), "hourly"
        )

        dpd_mock.get_volumes.assert_awaited_once()  # null stamps = gaps
        assert len(records) == 24
        assert all(r["dvstAlwrk"] is not None for r in records)
        by_day = {row.day: row.payload for row in await cache_rows()}
        assert len(by_day[YESTERDAY]) == 17  # nulls purged on rewrite
        assert all(r["dvstAlwrk"] is not None for r in by_day[YESTERDAY])

        dpd_mock.get_volumes.reset_mock()
        again = await fetch_dpd_volumes(
            [dev], as_dt(YESTERDAY), as_dt(YESTERDAY), "hourly"
        )
        dpd_mock.get_volumes.assert_not_awaited()
        assert len(again) == 24

    async def test_hourly_and_daily_cached_independently(self, dpd_mock):
        """Hourly and daily are unrelated DPD requests: same dates, separate
        cache entries, no coupling between their gaps."""
        devices = [make_device(101)]
        dpd_mock.get_volumes.return_value = hourly_records(
            devices, commercial_stamps(DAY1)
        )

        hourly = await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY1), "hourly")
        assert len(hourly) == 24
        assert await cache_row_count() == 2

        dpd_mock.get_volumes.reset_mock()
        dpd_mock.get_volumes.return_value = daily_records(devices, [DAY1])

        # Same day, other period_type → its own cache entry, so DPD is polled.
        await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY1), "daily")
        dpd_mock.get_volumes.assert_awaited_once()
        assert await cache_row_count() == 3

        # And the hourly entries still serve without a poll.
        dpd_mock.get_volumes.reset_mock()
        again = await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY1), "hourly")
        dpd_mock.get_volumes.assert_not_awaited()
        assert len(again) == 24


class TestDpdCacheEvents:
    async def test_events_cb_reports_phases_and_progress(self, dpd_mock):
        """The streaming endpoint's event feed: waiting → initial progress with
        the poll denominator → aggregating. Per-device increments are covered
        by the dpd_client unit test (get_volumes is mocked here)."""
        devices = [make_device(101), make_device(102)]
        dpd_mock.get_volumes.return_value = daily_records(devices, [DAY1])
        events = []

        await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY1), "daily",
                                events_cb=events.append)

        kinds = [(e.get("type"), e.get("phase")) for e in events]
        assert kinds[0] == ("status", "waiting")
        progress = [e for e in events if e["type"] == "progress"]
        assert progress[0] == {"type": "progress", "done": 0, "total": 2}
        assert ("status", "aggregating") in kinds

        # Full cache hit: no poll, no locks — no waiting phase either.
        dpd_mock.get_volumes.reset_mock()
        events.clear()
        await fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY1), "daily",
                                events_cb=events.append)
        dpd_mock.get_volumes.assert_not_awaited()
        assert [(e.get("type"), e.get("phase")) for e in events] == [
            ("progress", None),
            ("status", "aggregating"),
        ]
        assert events[0]["total"] == 0


class TestDpdStreamCancellation:
    async def test_cancelled_stream_releases_branch_lock(self, dpd_mock):
        """A client aborting the progress stream mid-poll must not leave the
        branch advisory lock behind (prod incident 2026-07-11: one cancelled
        stream wedged its branch for ~50 minutes). The detached reaper rolls
        the poll back, so the next request acquires the lock and completes."""
        devices = [make_device(101)]
        poll_started = asyncio.Event()
        never = asyncio.Event()

        async def hanging_get_volumes(polled, date_from, date_to, **kwargs):
            poll_started.set()
            await never.wait()  # hangs until the poll task is cancelled
            return []

        dpd_mock.get_volumes = hanging_get_volumes

        gen = EnterpriseRouter._volume_events(
            devices, as_dt(DAY1), as_dt(DAY1), "daily", None, False
        )
        first = json.loads(await asyncio.wait_for(gen.__anext__(), 5))
        assert first == {"type": "status", "phase": "waiting"}
        await asyncio.wait_for(poll_started.wait(), 5)  # lock is now held
        await gen.aclose()  # client disconnect

        async def quick_get_volumes(polled, date_from, date_to, **kwargs):
            return daily_records(polled, [DAY1])

        dpd_mock.get_volumes = quick_get_volumes
        records = await asyncio.wait_for(
            fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY1), "daily"),
            timeout=10,
        )
        assert record_keys(records) == {(101, DAY1.isoformat())}


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

    async def test_disjoint_device_sets_do_not_wait(self, dpd_mock):
        """Locks are per device: requests polling different devices of the
        same branch and period_type run in parallel."""
        dev_a, dev_b = make_device(101), make_device(102)
        a_started = asyncio.Event()
        release_a = asyncio.Event()

        async def get_volumes(polled, date_from, date_to, **kwargs):
            if polled[0]["serNum"] == 101:
                a_started.set()
                await release_a.wait()
            return daily_records(polled, [DAY1])

        dpd_mock.get_volumes = get_volumes
        task_a = asyncio.create_task(
            fetch_dpd_volumes([dev_a], as_dt(DAY1), as_dt(DAY1), "daily")
        )
        await asyncio.wait_for(a_started.wait(), 5)

        # B completes while A's poll still holds A's device lock.
        records_b = await asyncio.wait_for(
            fetch_dpd_volumes([dev_b], as_dt(DAY1), as_dt(DAY1), "daily"),
            timeout=5,
        )
        assert record_keys(records_b) == {(102, DAY1.isoformat())}

        release_a.set()
        records_a = await task_a
        assert record_keys(records_a) == {(101, DAY1.isoformat())}

    async def test_fully_cached_request_never_waits_for_a_running_poll(
        self, dpd_mock
    ):
        """A request served entirely from cache takes no locks at all — even
        when the very devices it reads are being re-polled by someone else."""
        dev = make_device(101)

        async def quick(polled, date_from, date_to, **kwargs):
            return daily_records(polled, [DAY1])

        dpd_mock.get_volumes = quick
        await fetch_dpd_volumes([dev], as_dt(DAY1), as_dt(DAY1), "daily")

        poll_started = asyncio.Event()
        release = asyncio.Event()

        async def hanging(polled, date_from, date_to, **kwargs):
            poll_started.set()
            await release.wait()
            return []

        dpd_mock.get_volumes = hanging
        # DAY2..DAY3 are missing → this poll hangs holding the device lock.
        wide_task = asyncio.create_task(
            fetch_dpd_volumes([dev], as_dt(DAY1), as_dt(DAY3), "daily")
        )
        await asyncio.wait_for(poll_started.wait(), 5)

        # DAY1 is fully cached → served without touching the lock.
        cached = await asyncio.wait_for(
            fetch_dpd_volumes([dev], as_dt(DAY1), as_dt(DAY1), "daily"),
            timeout=5,
        )
        assert record_keys(cached) == {(101, DAY1.isoformat())}

        release.set()
        await wide_task

    async def test_hourly_and_daily_polls_do_not_block_each_other(self, dpd_mock):
        """The lock includes period_type: a long hourly poll must not
        make a daily request for the same devices queue behind it."""
        devices = [make_device(101)]
        hourly_started = asyncio.Event()
        release_hourly = asyncio.Event()

        async def get_volumes(polled, date_from, date_to, **kwargs):
            if kwargs.get("type_request") == "hourly":
                hourly_started.set()
                await release_hourly.wait()
                return []
            return daily_records(polled, [DAY1])

        dpd_mock.get_volumes = get_volumes

        hourly_task = asyncio.create_task(
            fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY1), "hourly")
        )
        await asyncio.wait_for(hourly_started.wait(), 5)

        # Completes while the hourly poll still holds its own lock.
        records = await asyncio.wait_for(
            fetch_dpd_volumes(devices, as_dt(DAY1), as_dt(DAY1), "daily"),
            timeout=5,
        )
        assert record_keys(records) == {(101, DAY1.isoformat())}

        release_hourly.set()
        await hourly_task
