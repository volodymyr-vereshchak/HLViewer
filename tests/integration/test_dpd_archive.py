"""Integration tests for the scheduler-fed DPD archive (v4).

Model under test: the DB is the primary source. Reads inside a device's
coverage never touch the DPD API; ranges older than coverage are backfilled
on demand per device; the refresh job re-polls the last window for all
enterprises and prunes retention. DPDClient is mocked, Postgres is real."""

import asyncio
import json
from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text

from backend.api.endpoints.enterprise_ep import EnterpriseRouter
from backend.db.dao.dpd_archive_dao import DpdArchiveDao
from backend.db.engine import async_session_factory
from backend.db.models.enterprise_model import Enterprise
from backend.db.models.grmu_branch_model import GrmuBranch
from backend.services import dpd_archive_refresh
from backend.services.enterprise_volume_service import fetch_dpd_volumes

TODAY = date.today()
D_OLD10, D_OLD8, D_OLD5, D_OLD3 = (
    TODAY - timedelta(days=10), TODAY - timedelta(days=8),
    TODAY - timedelta(days=5), TODAY - timedelta(days=3),
)


def as_dt(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time())


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


@pytest_asyncio.fixture
async def branch_id(clean_db) -> int:
    async with async_session_factory() as session:
        branch = GrmuBranch(name="Тестова філія")
        session.add(branch)
        await session.commit()
        return branch.id


@pytest_asyncio.fixture
async def make_enterprise(branch_id):
    """Factory: creates an Enterprise row and returns the device dict the
    endpoints would build for it (id + quad + branch)."""
    async def _make(ser_num: int) -> dict:
        async with async_session_factory() as session:
            ent = Enterprise(
                enterprise_name=f"ent-{ser_num}", ser_num=ser_num,
                mf_dev=1, type_dev=3, ch_num=0,
                active=True, enabled=True, branch_id=branch_id, line_id=None,
            )
            session.add(ent)
            await session.commit()
            await session.refresh(ent)
        return {
            "id": ent.id, "line_id": 1, "branch_id": branch_id,
            "serNum": ser_num, "mfDev": 1, "typeDev": 3, "chNum": 0,
            "enterprise_name": ent.enterprise_name,
        }
    return _make


@pytest.fixture
def dpd_mock(mocker):
    """Patched DPDClient.for_branch (backfill path). Tests set
    .get_volumes.return_value / .side_effect as needed."""
    client = mocker.AsyncMock()
    mocker.patch(
        "backend.services.enterprise_volume_service.DPDClient.for_branch",
        mocker.AsyncMock(return_value=client),
    )
    return client


async def seed_archive(device: dict, period_type: str, days: list[date],
                       loaded_from: date) -> None:
    """Simulate a past scheduler run: store records + set coverage."""
    async with async_session_factory() as session:
        async with session.begin():
            dao = DpdArchiveDao(session)
            await dao.upsert_records(period_type, [
                {"enterprise_id": device["id"], "stamp": as_dt(day),
                 "dvst_alwrk": 10.0, "dvwrk_alwrk": None,
                 "press": 101.3, "temper": 15.0, "press_unit": "kPa"}
                for day in days
            ])
            await dao.lower_loaded_from([device["id"]], period_type, loaded_from)


async def archive_rows(period_type: str) -> list:
    table = "dpd_daily_archive" if period_type == "daily" else "dpd_hourly_archive"
    async with async_session_factory() as session:
        return (await session.execute(
            text(f"SELECT * FROM {table} ORDER BY enterprise_id")
        )).mappings().all()


async def coverage_of(ent_id: int, period_type: str):
    async with async_session_factory() as session:
        return (await session.execute(
            text("SELECT loaded_from FROM dpd_device_coverage "
                 "WHERE enterprise_id = :e AND period_type = :p"),
            {"e": ent_id, "p": period_type},
        )).scalar()


class TestArchiveReads:
    async def test_covered_range_served_from_db_without_dpd(
        self, dpd_mock, make_enterprise
    ):
        dev = await make_enterprise(101)
        await seed_archive(dev, "daily", [D_OLD5, D_OLD3], loaded_from=D_OLD10)

        records = await fetch_dpd_volumes([dev], as_dt(D_OLD10), as_dt(TODAY), "daily")

        dpd_mock.get_volumes.assert_not_awaited()  # DB is the sole source
        assert record_keys(records) == {
            (101, D_OLD5.isoformat()), (101, D_OLD3.isoformat()),
        }

    async def test_read_touches_accessed_at(self, dpd_mock, make_enterprise):
        dev = await make_enterprise(101)
        await seed_archive(dev, "daily", [D_OLD5], loaded_from=D_OLD10)
        async with async_session_factory() as session:
            await session.execute(text(
                "UPDATE dpd_daily_archive SET accessed_at = :old"
            ), {"old": TODAY - timedelta(days=6)})
            await session.commit()

        await fetch_dpd_volumes([dev], as_dt(D_OLD5), as_dt(D_OLD5), "daily")

        rows = await archive_rows("daily")
        assert rows[0]["accessed_at"] == TODAY

    async def test_hourly_commercial_window(self, dpd_mock, make_enterprise):
        """from=D1&to=D1 hourly means the commercial day [D1 07:00..D2 06:00]."""
        dev = await make_enterprise(101)
        stamps = [as_dt(D_OLD5) + timedelta(hours=7 + i) for i in range(24)]
        async with async_session_factory() as session:
            async with session.begin():
                dao = DpdArchiveDao(session)
                await dao.upsert_records("hourly", [
                    {"enterprise_id": dev["id"], "stamp": s,
                     "dvst_alwrk": 1.0, "dvwrk_alwrk": None,
                     "press": None, "temper": None, "press_unit": None}
                    for s in stamps
                ])
                await dao.lower_loaded_from([dev["id"]], "hourly", D_OLD10)

        records = await fetch_dpd_volumes([dev], as_dt(D_OLD5), as_dt(D_OLD5), "hourly")

        dpd_mock.get_volumes.assert_not_awaited()
        got = sorted(r["date"] for r in records)
        assert len(got) == 24
        assert got[0] == (as_dt(D_OLD5) + timedelta(hours=7)).isoformat()
        assert got[-1] == (as_dt(D_OLD5) + timedelta(days=1, hours=6)).isoformat()


class TestBackfill:
    async def test_range_older_than_coverage_backfills_per_device(
        self, dpd_mock, make_enterprise
    ):
        dev = await make_enterprise(101)
        await seed_archive(dev, "daily", [D_OLD5, D_OLD3], loaded_from=D_OLD5)
        dpd_mock.get_volumes.return_value = daily_records([dev], [D_OLD8])

        records = await fetch_dpd_volumes(
            [dev], as_dt(D_OLD10), as_dt(D_OLD3), "daily"
        )

        dpd_mock.get_volumes.assert_awaited_once()
        ranges = dpd_mock.get_volumes.await_args.kwargs["device_ranges"]
        quad = (101, 1, 3, 0)
        # Only the uncovered head [requested .. loaded_from-1] is fetched.
        assert ranges[quad] == (as_dt(D_OLD10), as_dt(D_OLD5 - timedelta(days=1)))
        assert record_keys(records) == {
            (101, D_OLD8.isoformat()), (101, D_OLD5.isoformat()),
            (101, D_OLD3.isoformat()),
        }
        # Coverage lowered → the same range is DB-only from now on,
        # including the stretches DPD had nothing for.
        assert await coverage_of(dev["id"], "daily") == D_OLD10
        dpd_mock.get_volumes.reset_mock()
        again = await fetch_dpd_volumes(
            [dev], as_dt(D_OLD10), as_dt(D_OLD3), "daily"
        )
        dpd_mock.get_volumes.assert_not_awaited()
        assert record_keys(again) == record_keys(records)

    async def test_never_fetched_device_backfills_whole_range(
        self, dpd_mock, make_enterprise
    ):
        dev = await make_enterprise(101)  # no coverage row at all
        dpd_mock.get_volumes.return_value = daily_records([dev], [D_OLD5])

        records = await fetch_dpd_volumes(
            [dev], as_dt(D_OLD5), as_dt(D_OLD3), "daily"
        )

        dpd_mock.get_volumes.assert_awaited_once()
        ranges = dpd_mock.get_volumes.await_args.kwargs["device_ranges"]
        assert ranges[(101, 1, 3, 0)] == (as_dt(D_OLD5), as_dt(D_OLD3))
        assert record_keys(records) == {(101, D_OLD5.isoformat())}

    async def test_skeleton_records_not_stored(self, dpd_mock, make_enterprise):
        dev = await make_enterprise(101)
        recs = daily_records([dev], [D_OLD5, D_OLD3])
        recs[1]["dvstAlwrk"] = None  # skeleton: no data for D_OLD3
        dpd_mock.get_volumes.return_value = recs

        await fetch_dpd_volumes([dev], as_dt(D_OLD5), as_dt(D_OLD3), "daily")

        rows = await archive_rows("daily")
        assert len(rows) == 1
        assert rows[0]["day"] == D_OLD5

    async def test_backfill_only_missing_devices(self, dpd_mock, make_enterprise):
        dev_a = await make_enterprise(101)
        dev_b = await make_enterprise(102)
        await seed_archive(dev_a, "daily", [D_OLD8, D_OLD5], loaded_from=D_OLD10)
        dpd_mock.get_volumes.return_value = daily_records([dev_b], [D_OLD5])

        await fetch_dpd_volumes(
            [dev_a, dev_b], as_dt(D_OLD10), as_dt(D_OLD5), "daily"
        )

        dpd_mock.get_volumes.assert_awaited_once()
        polled = dpd_mock.get_volumes.await_args.args[0]
        assert [d["serNum"] for d in polled] == [102]  # A is covered

    async def test_events_progress_on_backfill_and_none_on_db_read(
        self, dpd_mock, make_enterprise
    ):
        dev = await make_enterprise(101)
        dpd_mock.get_volumes.return_value = daily_records([dev], [D_OLD5])
        events = []

        await fetch_dpd_volumes([dev], as_dt(D_OLD5), as_dt(D_OLD5), "daily",
                                events_cb=events.append)
        kinds = [(e.get("type"), e.get("phase")) for e in events]
        assert ("progress", None) in kinds  # backfill of 1 device
        assert events[0] == {"type": "progress", "done": 0, "total": 1}
        assert kinds[-1] == ("status", "aggregating")

        events.clear()
        dpd_mock.get_volumes.reset_mock()
        await fetch_dpd_volumes([dev], as_dt(D_OLD5), as_dt(D_OLD5), "daily",
                                events_cb=events.append)
        dpd_mock.get_volumes.assert_not_awaited()
        assert [(e.get("type"), e.get("phase")) for e in events] == [
            ("progress", None),  # total=0 — nothing to backfill
            ("status", "aggregating"),
        ]
        assert events[0]["total"] == 0

    async def test_concurrent_backfill_dedup(self, dpd_mock, make_enterprise):
        """Two identical uncovered requests: the follower waits on the device
        lock, re-reads coverage and skips its own DPD call."""
        dev = await make_enterprise(101)
        started = asyncio.Event()
        release = asyncio.Event()
        calls = {"count": 0}

        async def slow_get_volumes(polled, date_from, date_to, **kwargs):
            calls["count"] += 1
            started.set()
            await release.wait()
            return daily_records(polled, [D_OLD5])

        dpd_mock.get_volumes = slow_get_volumes
        leader = asyncio.create_task(
            fetch_dpd_volumes([dev], as_dt(D_OLD5), as_dt(D_OLD3), "daily")
        )
        await asyncio.wait_for(started.wait(), 5)
        follower = asyncio.create_task(
            fetch_dpd_volumes([dev], as_dt(D_OLD5), as_dt(D_OLD3), "daily")
        )
        await asyncio.sleep(0.3)
        release.set()

        first, second = await asyncio.gather(leader, follower)
        assert calls["count"] == 1
        assert record_keys(first) == record_keys(second)


class TestRetention:
    async def test_prune_old_unread_rows_and_raise_coverage(
        self, dpd_mock, make_enterprise
    ):
        dev = await make_enterprise(101)
        ancient = TODAY - timedelta(days=400)
        recent = D_OLD5
        await seed_archive(dev, "daily", [ancient, recent],
                           loaded_from=ancient)
        async with async_session_factory() as session:
            # Nobody has read anything for 8 days.
            await session.execute(text(
                "UPDATE dpd_daily_archive SET accessed_at = :old"
            ), {"old": TODAY - timedelta(days=8)})
            await session.commit()

        async with async_session_factory() as session:
            async with session.begin():
                pruned = await DpdArchiveDao(session).prune("daily")

        assert pruned == 1
        rows = await archive_rows("daily")
        assert [r["day"] for r in rows] == [recent]  # only the year-old one went
        # Coverage raised to the horizon → the range is backfillable again.
        assert await coverage_of(dev["id"], "daily") == TODAY - timedelta(days=365)

    async def test_recently_read_old_rows_survive(self, dpd_mock, make_enterprise):
        dev = await make_enterprise(101)
        ancient = TODAY - timedelta(days=400)
        await seed_archive(dev, "daily", [ancient], loaded_from=ancient)
        # accessed_at = today (set by upsert) → inside the 7-day grace.

        async with async_session_factory() as session:
            async with session.begin():
                pruned = await DpdArchiveDao(session).prune("daily")

        assert pruned == 0
        assert len(await archive_rows("daily")) == 1


class TestRefreshJob:
    async def test_refresh_polls_window_and_updates_coverage(
        self, mocker, make_enterprise, branch_id
    ):
        dev = await make_enterprise(101)
        client = mocker.AsyncMock()

        async def get_volumes(devices, date_from, date_to, *, type_request,
                              **kwargs):
            if type_request == "daily":
                return daily_records(devices, [D_OLD5, D_OLD3])
            return [{
                "serNum": d["serNum"], "mfDev": d["mfDev"],
                "typeDev": d["typeDev"], "chNum": d["chNum"],
                "date": f"{D_OLD5.isoformat()}T{h:02d}:00:00", "dvstAlwrk": 1.0,
            } for d in devices for h in range(3)]

        client.get_volumes = get_volumes
        mocker.patch(
            "backend.services.dpd_archive_refresh.DPDClient.for_branch",
            mocker.AsyncMock(return_value=client),
        )
        mocker.patch(
            "backend.services.dpd_archive_refresh._branch_ids_with_credentials",
            mocker.AsyncMock(return_value=[branch_id]),
        )

        ran = await dpd_archive_refresh.run_refresh()

        assert ran is True
        assert len(await archive_rows("daily")) == 2
        assert len(await archive_rows("hourly")) == 3
        window_from = TODAY - timedelta(days=30)
        assert await coverage_of(dev["id"], "daily") == window_from
        assert await coverage_of(dev["id"], "hourly") == window_from
        status = await dpd_archive_refresh.read_status()
        assert status["status"] == "done"

    async def test_refresh_reports_progress(
        self, mocker, make_enterprise, branch_id
    ):
        """A running refresh exposes progress_done/progress_total (devices ×2,
        daily + hourly) for the admin progress bar and clears them on finish."""
        await make_enterprise(101)
        await make_enterprise(102)
        mid_status = {}

        async def get_volumes(devices, date_from, date_to, *, type_request,
                              progress_cb=None, **kwargs):
            assert progress_cb is not None
            progress_cb(len(devices), len(devices))  # all devices polled
            if type_request == "daily":
                # The progress write is a detached throttled task — wait for
                # it, then snapshot what an admin status poll sees mid-run.
                for _ in range(100):
                    await asyncio.sleep(0.05)
                    s = await dpd_archive_refresh.read_status()
                    if s["progress_done"] == 2:
                        break
                mid_status.update(s)
            return []

        client = mocker.AsyncMock()
        client.get_volumes = get_volumes
        mocker.patch(
            "backend.services.dpd_archive_refresh.DPDClient.for_branch",
            mocker.AsyncMock(return_value=client),
        )
        mocker.patch(
            "backend.services.dpd_archive_refresh._branch_ids_with_credentials",
            mocker.AsyncMock(return_value=[branch_id]),
        )

        assert await dpd_archive_refresh.run_refresh() is True

        assert mid_status["status"] == "running"
        assert mid_status["progress_total"] == 4  # 2 devices × 2 period types
        assert mid_status["progress_done"] == 2   # daily pass finished
        final = await dpd_archive_refresh.read_status()
        assert final["status"] == "done"
        assert final["progress_done"] is None
        assert final["progress_total"] is None

    async def test_refresh_lock_rejects_second_run(self, clean_db):
        assert await dpd_archive_refresh.acquire() is True
        # While running, another trigger must be refused.
        assert await dpd_archive_refresh.acquire() is False
        ran = await dpd_archive_refresh.run_refresh()
        assert ran is False
        await dpd_archive_refresh._finalize("done", None)


class TestStreamCancellation:
    async def test_cancelled_stream_releases_backfill_locks(
        self, dpd_mock, make_enterprise
    ):
        """A client aborting the stream mid-backfill must not leave device
        locks behind: the next identical request completes."""
        dev = await make_enterprise(101)
        started = asyncio.Event()
        never = asyncio.Event()

        async def hanging(polled, date_from, date_to, **kwargs):
            started.set()
            await never.wait()
            return []

        dpd_mock.get_volumes = hanging
        gen = EnterpriseRouter._volume_events(
            [dev], as_dt(D_OLD5), as_dt(D_OLD5), "daily", None, False
        )
        first = json.loads(await asyncio.wait_for(gen.__anext__(), 5))
        assert first["type"] in ("progress", "status")  # stream is live
        await asyncio.wait_for(started.wait(), 5)
        await gen.aclose()  # client disconnect

        async def quick(polled, date_from, date_to, **kwargs):
            return daily_records(polled, [D_OLD5])

        dpd_mock.get_volumes = quick
        records = await asyncio.wait_for(
            fetch_dpd_volumes([dev], as_dt(D_OLD5), as_dt(D_OLD5), "daily"),
            timeout=10,
        )
        assert record_keys(records) == {(101, D_OLD5.isoformat())}
