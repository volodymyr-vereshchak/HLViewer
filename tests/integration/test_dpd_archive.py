"""Integration tests for the scheduler-fed DPD archive (v4).

Model under test: the DB is the ONLY source of a read — whatever the range,
no read contacts the DPD API. The archive is written by the two writers that
are asked to write: the refresh job, which re-polls the last window for every
enterprise, and an explicit poll (live=True). DPDClient is mocked, Postgres is
real."""

import asyncio
import json
from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy import text as sa_text

from backend.api.endpoints.enterprise_ep import EnterpriseRouter
from backend.db.dao.dpd_archive_dao import DpdArchiveDao
from backend.db.engine import async_session_factory
from backend.db.models.enterprise_model import (
    DpdDevice, Enterprise, EnterpriseDevice, EPOCH_INSTALLED_FROM,
)
from backend.db.models.grmu_branch_model import GrmuBranch
from backend.hl_engine.scheduler_runner import _last_due_refresh
from backend.services import archive_cleanup, dpd_archive_refresh
from backend.services.enterprise_volume_service import fetch_dpd_volumes

TODAY = date.today()
D_OLD10, D_OLD8, D_OLD5, D_OLD3 = (
    TODAY - timedelta(days=10), TODAY - timedelta(days=8),
    TODAY - timedelta(days=5), TODAY - timedelta(days=3),
)


def as_dt(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time())


def daily_records(devices: list[dict], days: list[date]) -> list[dict]:
    """What the DPD client hands back.

    `tag` is copied straight off the device dict the client was handed — that
    is exactly what the real client does, and it is the only faithful mock:
    reading the tag from any other field would quietly decide for the code
    under test what it tags its requests with."""
    return [
        {
            "tag": d.get("tag"),
            "serNum": d["serNum"], "mfDev": d["mfDev"], "typeDev": d["typeDev"],
            "chNum": d["chNum"], "date": day.isoformat(), "dvstAlwrk": 10.0,
            "press": 101.3, "temper": 15.0,
        }
        for d in devices
        for day in days
    ]


def daily_reply(days: list[date], blank: list[date] = ()):
    """`get_volumes` side effect: answer with `days` for whichever devices were
    actually polled.

    A side effect rather than a return_value on purpose. The records have to be
    built from the device dicts the client was handed, or the mock would supply
    the tag itself and the test could no longer tell whether the code tags its
    requests correctly. `blank` lists days answered as skeletons (a stamp with
    no readings yet), which DPD does send and the archive must not store."""
    async def _reply(devices, date_from, date_to, **kwargs):
        records = daily_records(devices, list(days) + list(blank))
        for record in records:
            if date.fromisoformat(record["date"]) in blank:
                record["dvstAlwrk"] = None
        return records

    return _reply


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
    """Factory: creates a metering point with one corrector standing there
    since forever, and returns the ASSIGNMENT dict the endpoints build.

    `installed_from` defaults to the epoch, which is what every point migrated
    from the pre-history schema looks like — so these tests exercise the same
    shape production carries right after the upgrade.

    The id sequences are pushed apart first. In a truncated database the first
    enterprise, device and assignment would all be id 1, and a test could not
    tell "tagged by device" from "tagged by assignment" — both would pass."""
    async with async_session_factory() as session:
        await session.execute(text("ALTER SEQUENCE dpd_device_id_seq RESTART WITH 1000"))
        await session.execute(
            text("ALTER SEQUENCE enterprise_device_id_seq RESTART WITH 500")
        )
        await session.commit()

    async def _make(ser_num: int, installed_from=None, removed_at=None) -> dict:
        async with async_session_factory() as session:
            ent = Enterprise(
                enterprise_name=f"ent-{ser_num}",
                active=True, enabled=True, branch_id=branch_id, line_id=None,
            )
            session.add(ent)
            await session.flush()
            device = DpdDevice(ser_num=ser_num, mf_dev=1, type_dev=3, ch_num=0)
            session.add(device)
            await session.flush()
            entry = EnterpriseDevice(
                enterprise_id=ent.id, device_id=device.id,
                installed_from=installed_from or EPOCH_INSTALLED_FROM,
                removed_at=removed_at,
            )
            session.add(entry)
            await session.commit()
            await session.refresh(ent)
            await session.refresh(device)
            await session.refresh(entry)
        return {
            "enterprise_id": ent.id, "assignment_id": entry.id,
            "device_id": device.id,
            "line_id": 1, "branch_id": branch_id,
            "serNum": ser_num, "mfDev": 1, "typeDev": 3, "chNum": 0,
            "enterprise_name": ent.enterprise_name,
            "win_from": entry.installed_from, "win_to": removed_at,
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


async def seed_archive(device: dict, period_type: str,
                       days: list[date]) -> None:
    """Simulate a past scheduler run: store records."""
    async with async_session_factory() as session:
        async with session.begin():
            await DpdArchiveDao(session).upsert_records(period_type, [
                {"device_id": device["device_id"], "stamp": as_dt(day),
                 "dvst_alwrk": 10.0, "dvwrk_alwrk": None,
                 "press": 101.3, "temper": 15.0, "press_unit": "kPa"}
                for day in days
            ])


async def archive_rows(period_type: str) -> list:
    table = "dpd_daily_archive" if period_type == "daily" else "dpd_hourly_archive"
    async with async_session_factory() as session:
        return (await session.execute(
            text(f"SELECT * FROM {table} ORDER BY device_id")
        )).mappings().all()


class TestArchiveReads:
    async def test_covered_range_served_from_db_without_dpd(
        self, dpd_mock, make_enterprise
    ):
        dev = await make_enterprise(101)
        await seed_archive(dev, "daily", [D_OLD5, D_OLD3])

        records = await fetch_dpd_volumes([dev], as_dt(D_OLD10), as_dt(TODAY), "daily")

        dpd_mock.get_volumes.assert_not_awaited()  # DB is the sole source
        assert record_keys(records) == {
            (101, D_OLD5.isoformat()), (101, D_OLD3.isoformat()),
        }

    async def test_hourly_commercial_window(self, dpd_mock, make_enterprise):
        """from=D1&to=D1 hourly means the commercial day [D1 07:00..D2 06:00]."""
        dev = await make_enterprise(101)
        stamps = [as_dt(D_OLD5) + timedelta(hours=7 + i) for i in range(24)]
        async with async_session_factory() as session:
            async with session.begin():
                dao = DpdArchiveDao(session)
                await dao.upsert_records("hourly", [
                    {"device_id": dev["device_id"], "stamp": s,
                     "dvst_alwrk": 1.0, "dvwrk_alwrk": None,
                     "press": None, "temper": None, "press_unit": None}
                    for s in stamps
                ])

        records = await fetch_dpd_volumes([dev], as_dt(D_OLD5), as_dt(D_OLD5), "hourly")

        dpd_mock.get_volumes.assert_not_awaited()
        got = sorted(r["date"] for r in records)
        assert len(got) == 24
        assert got[0] == (as_dt(D_OLD5) + timedelta(hours=7)).isoformat()
        assert got[-1] == (as_dt(D_OLD5) + timedelta(days=1, hours=6)).isoformat()


class TestAReadNeverCallsTheApi:
    """What made the same September report come out twice differently.

    A range older than a device's coverage used to be fetched on demand. The
    API answers per device and does not raise when some of them fail — a
    timeout, a 404 and an auth error all arrive as "no records" — and coverage
    was then lowered for the whole batch anyway. The point whose corrector had
    timed out was read from an empty archive ever after, so the night report
    subtracted nothing for it and called the whole line population; when the
    scheduler later wrote those hours, the same export subtracted again.
    """

    async def test_a_range_older_than_coverage_is_not_fetched(
        self, dpd_mock, make_enterprise
    ):
        dev = await make_enterprise(101)
        await seed_archive(dev, "daily", [D_OLD5, D_OLD3])

        records = await fetch_dpd_volumes(
            [dev], as_dt(D_OLD10), as_dt(D_OLD3), "daily"
        )

        dpd_mock.get_volumes.assert_not_awaited()
        # Only what the archive holds. The older head is simply absent.
        assert record_keys(records) == {
            (101, D_OLD5.isoformat()), (101, D_OLD3.isoformat()),
        }

    async def test_a_device_never_fetched_reads_as_nothing(
        self, dpd_mock, make_enterprise
    ):
        """Visibly nothing, which the report says out loud — not a number
        quietly fetched behind the reader's back."""
        dev = await make_enterprise(101)  # no coverage row, no rows

        records = await fetch_dpd_volumes(
            [dev], as_dt(D_OLD5), as_dt(D_OLD3), "daily"
        )

        dpd_mock.get_volumes.assert_not_awaited()
        assert records == []

    async def test_a_read_waits_for_nothing(self, dpd_mock, make_enterprise):
        """No locks, no progress: there is nothing to make progress through."""
        dev = await make_enterprise(101)
        await seed_archive(dev, "daily", [D_OLD5])
        events = []

        await fetch_dpd_volumes([dev], as_dt(D_OLD5), as_dt(D_OLD5), "daily",
                                events_cb=events.append)

        assert [(e.get("type"), e.get("phase")) for e in events] == [
            ("status", "aggregating"),
        ]


class TestAPollDoesCallTheApi:
    """`live=True` — the «Опитати» button, the one place a read of the screen
    turns into a request to DPD."""

    async def test_the_whole_range_is_polled_and_stored(
        self, dpd_mock, make_enterprise
    ):
        dev = await make_enterprise(101)
        dpd_mock.get_volumes.side_effect = daily_reply([D_OLD8, D_OLD5])

        records = await fetch_dpd_volumes(
            [dev], as_dt(D_OLD10), as_dt(D_OLD3), "daily", live=True
        )

        dpd_mock.get_volumes.assert_awaited_once()
        polled = dpd_mock.get_volumes.await_args.args[0]
        assert polled[0]["range"] == (as_dt(D_OLD10), as_dt(D_OLD3))
        assert record_keys(records) == {
            (101, D_OLD8.isoformat()), (101, D_OLD5.isoformat()),
        }
        # And it is in the archive for the next reader, who will not poll.
        dpd_mock.get_volumes.reset_mock()
        again = await fetch_dpd_volumes(
            [dev], as_dt(D_OLD10), as_dt(D_OLD3), "daily"
        )
        dpd_mock.get_volumes.assert_not_awaited()
        assert record_keys(again) == record_keys(records)

    async def test_skeleton_records_not_stored(self, dpd_mock, make_enterprise):
        dev = await make_enterprise(101)
        dpd_mock.get_volumes.side_effect = daily_reply([D_OLD5], blank=[D_OLD3])

        await fetch_dpd_volumes([dev], as_dt(D_OLD5), as_dt(D_OLD3), "daily",
                                live=True)

        rows = await archive_rows("daily")
        assert len(rows) == 1
        assert rows[0]["day"] == D_OLD5

    async def test_every_device_of_the_request_is_polled(
        self, dpd_mock, make_enterprise
    ):
        """A poll is asked for, so it asks about everything — what is already
        in the archive is what it is meant to refresh."""
        dev_a = await make_enterprise(101)
        dev_b = await make_enterprise(102)
        await seed_archive(dev_a, "daily", [D_OLD8, D_OLD5])
        dpd_mock.get_volumes.side_effect = daily_reply([D_OLD5])

        await fetch_dpd_volumes(
            [dev_a, dev_b], as_dt(D_OLD10), as_dt(D_OLD5), "daily", live=True
        )

        dpd_mock.get_volumes.assert_awaited_once()
        polled = dpd_mock.get_volumes.await_args.args[0]
        assert sorted(d["serNum"] for d in polled) == [101, 102]

    async def test_progress_is_reported(self, dpd_mock, make_enterprise):
        dev = await make_enterprise(101)
        dpd_mock.get_volumes.side_effect = daily_reply([D_OLD5])
        events = []

        await fetch_dpd_volumes([dev], as_dt(D_OLD5), as_dt(D_OLD5), "daily",
                                events_cb=events.append, live=True)

        kinds = [(e.get("type"), e.get("phase")) for e in events]
        assert events[0] == {"type": "progress", "done": 0, "total": 1}
        assert ("status", "waiting") in kinds
        assert kinds[-1] == ("status", "aggregating")

    async def test_a_failed_poll_still_serves_the_archive(
        self, dpd_mock, make_enterprise
    ):
        """The API being down must not empty a screen the DB can fill."""
        dev = await make_enterprise(101)
        await seed_archive(dev, "daily", [D_OLD5])
        dpd_mock.get_volumes.side_effect = RuntimeError("DPD не відповідає")

        records = await fetch_dpd_volumes(
            [dev], as_dt(D_OLD5), as_dt(D_OLD5), "daily", live=True
        )

        assert record_keys(records) == {(101, D_OLD5.isoformat())}

    async def test_two_polls_of_one_device_do_not_deadlock(
        self, dpd_mock, make_enterprise
    ):
        """They serialize on the device lock and both finish."""
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
        leader = asyncio.create_task(fetch_dpd_volumes(
            [dev], as_dt(D_OLD5), as_dt(D_OLD3), "daily", live=True))
        await asyncio.wait_for(started.wait(), 5)
        follower = asyncio.create_task(fetch_dpd_volumes(
            [dev], as_dt(D_OLD5), as_dt(D_OLD3), "daily", live=True))
        await asyncio.sleep(0.3)
        release.set()

        first, second = await asyncio.gather(leader, follower)
        assert calls["count"] == 2
        assert record_keys(first) == record_keys(second)


class TestRetentionIsOff:
    """Nothing is ever deleted from these archives (07.09.2026).

    They were a cache while the DPD API was the only source and a dropped row
    could be re-fetched. A row the GSM poll writes has no second source — the
    device keeps weeks, not years — so keeping everything is cheaper than a
    job that removes what nobody can restore.
    """

    async def test_a_year_old_record_that_nobody_reads_survives(
        self, dpd_mock, make_enterprise
    ):
        dev = await make_enterprise(101)
        ancient = TODAY - timedelta(days=400)
        await seed_archive(dev, "daily", [ancient, D_OLD5])

        rows = await archive_rows("daily")
        assert sorted(r["day"] for r in rows) == sorted([ancient, D_OLD5])


class TestSource:
    """UNIQUE(device_id, stamp) leaves room for one row per period, so "the
    GSM poll is the truth" has to be an UPSERT rule rather than a read-time
    preference: otherwise the nightly DPD refresh overwrites what the modem
    read off the device."""

    async def _poll_over_gsm(self, dev, volume):
        async with async_session_factory() as session:
            async with session.begin():
                await DpdArchiveDao(session).upsert_records("daily", [
                    {"device_id": dev["device_id"], "stamp": as_dt(D_OLD5),
                     "dvst_alwrk": volume, "dvwrk_alwrk": None,
                     "press": None, "temper": None, "press_unit": None},
                ], source="gsm")

    async def _poll_over_gsm_with_unit(self, dev, unit):
        async with async_session_factory() as session:
            async with session.begin():
                await DpdArchiveDao(session).upsert_records("daily", [
                    {"device_id": dev["device_id"], "stamp": as_dt(D_OLD5),
                     "dvst_alwrk": 1.0, "dvwrk_alwrk": None,
                     "press": 0.15, "temper": None, "press_unit": unit},
                ], source="gsm")

    async def test_the_api_refresh_marks_its_rows(self, dpd_mock, make_enterprise):
        dev = await make_enterprise(101)
        await seed_archive(dev, "daily", [D_OLD5])
        assert (await archive_rows("daily"))[0]["source"] == "dpd"

    async def test_a_gsm_row_survives_a_dpd_refresh(self, dpd_mock, make_enterprise):
        dev = await make_enterprise(101)
        await self._poll_over_gsm(dev, 777.0)

        await seed_archive(dev, "daily", [D_OLD5])

        row = (await archive_rows("daily"))[0]
        assert row["source"] == "gsm"
        assert row["dvst_alwrk"] == 777.0, "the modem's reading must stand"

    async def test_a_gsm_poll_overwrites_what_the_api_left(
        self, dpd_mock, make_enterprise
    ):
        dev = await make_enterprise(101)
        await seed_archive(dev, "daily", [D_OLD5])
        await self._poll_over_gsm(dev, 777.0)

        row = (await archive_rows("daily"))[0]
        assert (row["source"], row["dvst_alwrk"]) == ("gsm", 777.0)

    async def test_a_second_gsm_poll_still_wins(self, dpd_mock, make_enterprise):
        # Re-polling the same period must update it, or a corrected reading
        # could never replace a wrong one.
        dev = await make_enterprise(101)
        await self._poll_over_gsm(dev, 777.0)
        await self._poll_over_gsm(dev, 888.0)
        assert (await archive_rows("daily"))[0]["dvst_alwrk"] == 888.0

    async def test_a_poll_that_does_not_know_the_unit_does_not_erase_it(
        self, dpd_mock, make_enterprise
    ):
        """The unit is the one column a writer may legitimately not know.

        A modem reads the number a corrector stores and, for most families,
        nothing that says what it is in. ДПД does report it — and if the poll
        wrote its silence over that, the row would be left unreadable: 0.15
        under a column captioned кгс/см² instead of МПа, a factor of ten.
        """
        dev = await make_enterprise(101)
        await seed_archive(dev, "daily", [D_OLD5])
        async with async_session_factory() as session:
            async with session.begin():
                await DpdArchiveDao(session).upsert_records("daily", [
                    {"device_id": dev["device_id"], "stamp": as_dt(D_OLD5),
                     "dvst_alwrk": 1.0, "dvwrk_alwrk": None, "press": 0.15,
                     "temper": None, "press_unit": "МПа"},
                ], source="dpd")

        await self._poll_over_gsm(dev, 777.0)

        row = (await archive_rows("daily"))[0]
        assert (row["source"], row["dvst_alwrk"]) == ("gsm", 777.0)
        assert row["press_unit"] == "МПа", "the unit ДПД reported must stand"

    async def test_a_poll_that_does_know_the_unit_says_so(
        self, dpd_mock, make_enterprise
    ):
        """A КПЛГ stores kgf/cm² and the agent converts, so that one is known
        and is sent — otherwise the row is read under the line's default."""
        dev = await make_enterprise(101)
        await self._poll_over_gsm_with_unit(dev, "МПа")
        assert (await archive_rows("daily"))[0]["press_unit"] == "МПа"


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
                "tag": d.get("tag"),
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
        status = await dpd_archive_refresh.read_status()
        assert status["status"] == "done"

    async def test_shared_corrector_is_polled_once_as_a_device(
        self, mocker, make_enterprise, branch_id
    ):
        """A corrector moved between two points is one device with one
        archive: it is asked once per period type, over its own stretch, not
        once per point. Which point reads which part is settled on the read,
        so the refresh has no reason to know about the windows at all."""
        first = await make_enterprise(101, removed_at=as_dt(D_OLD5))
        async with async_session_factory() as session:
            second = Enterprise(
                enterprise_name="ent-101-moved", active=True, enabled=True,
                branch_id=branch_id, line_id=None,
            )
            session.add(second)
            await session.flush()
            session.add(EnterpriseDevice(
                enterprise_id=second.id, device_id=first["device_id"],
                installed_from=as_dt(D_OLD5),
            ))
            await session.commit()

        # A stretch already stored, so the device needs one chunk and the
        # count below is about points, not about how long the stretch is.
        await seed_archive(first, "daily", [D_OLD10])

        calls = []
        client = mocker.AsyncMock()

        async def get_volumes(devices, date_from, date_to, *, type_request, **kw):
            calls.append((type_request, [d["tag"] for d in devices],
                          date_from, date_to))
            return daily_records(devices, [D_OLD3]) if type_request == "daily" else []

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

        daily = [c for c in calls if c[0] == "daily"]
        assert len(daily) == 1
        assert daily[0][1] == [first["device_id"]]  # once, as a device
        # One row for one device, not one per point it served. D_OLD10 was
        # seeded, D_OLD3 came back from the poll.
        assert sorted(r["day"] for r in await archive_rows("daily")) == [
            D_OLD10, D_OLD3]
        status = await dpd_archive_refresh.read_status()
        assert status["progress_total"] is None  # cleared on finish

    async def test_refresh_reports_progress(
        self, mocker, make_enterprise, branch_id
    ):
        """A running refresh exposes progress_done/progress_total for the
        admin progress bar and clears them on finish. The unit is POLLS: a
        device needs one per period type while it is up to date, and more when
        it has a long stretch to catch up on."""
        for code in (101, 102):
            dev = await make_enterprise(code)
            # Up to date, so each device is exactly one poll per period type.
            await seed_archive(dev, "daily", [D_OLD3])
            await seed_archive(dev, "hourly", [D_OLD3])
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
    async def test_cancelled_stream_releases_the_device_locks(
        self, dpd_mock, make_enterprise
    ):
        """A client aborting the stream mid-poll must not leave device locks
        behind: the next poll of the same device completes."""
        dev = await make_enterprise(101)
        started = asyncio.Event()
        never = asyncio.Event()

        async def hanging(polled, date_from, date_to, **kwargs):
            started.set()
            await never.wait()
            return []

        dpd_mock.get_volumes = hanging
        gen = EnterpriseRouter._volume_events(
            [dev], as_dt(D_OLD5), as_dt(D_OLD5), "daily", None, False,
            True, True,          # include_devices, live — only a poll locks
        )
        first = json.loads(await asyncio.wait_for(gen.__anext__(), 5))
        assert first["type"] in ("progress", "status")  # stream is live
        await asyncio.wait_for(started.wait(), 5)
        await gen.aclose()  # client disconnect

        async def quick(polled, date_from, date_to, **kwargs):
            return daily_records(polled, [D_OLD5])

        dpd_mock.get_volumes = quick
        records = await asyncio.wait_for(
            fetch_dpd_volumes([dev], as_dt(D_OLD5), as_dt(D_OLD5), "daily",
                              live=True),
            timeout=10,
        )
        assert record_keys(records) == {(101, D_OLD5.isoformat())}


class TestRereadingOverAChosenPeriod:
    """How much each run asks for.

    The routine run carries every device on from the newest period its own
    archive holds, and reads one that holds nothing from FIRST_EVER. It
    therefore never looks for a hole in the middle: a read of the archive
    never calls the API, so a month the server was off stays empty until
    «Перечитати архів» asks for exactly that period.
    """

    async def _mock_branch(self, mocker, branch_id, seen):
        client = mocker.AsyncMock()

        async def get_volumes(devices, date_from, date_to, *, type_request,
                              **kwargs):
            # The range that counts is each device's own; date_from/date_to
            # only bound the batch.
            for device in devices:
                seen.append((type_request, device["tag"], *device["range"]))
            return daily_records(devices, [D_OLD5]) if type_request == "daily" else []

        client.get_volumes = get_volumes
        mocker.patch(
            "backend.services.dpd_archive_refresh.DPDClient.for_branch",
            mocker.AsyncMock(return_value=client),
        )
        mocker.patch(
            "backend.services.dpd_archive_refresh._branch_ids_with_credentials",
            mocker.AsyncMock(return_value=[branch_id]),
        )

    @staticmethod
    def _daily(seen):
        return [s for s in seen if s[0] == "daily"]

    async def test_the_period_asked_for_is_the_period_polled(
        self, mocker, make_enterprise, branch_id
    ):
        await make_enterprise(101)
        seen: list = []
        await self._mock_branch(mocker, branch_id, seen)
        since = date(2024, 1, 1)

        await dpd_archive_refresh.execute_locked(since, TODAY)

        daily = self._daily(seen)
        assert daily[0][2] == as_dt(since)
        assert daily[-1][3].date() == TODAY

    async def test_a_dated_reread_ignores_what_is_already_stored(
        self, mocker, make_enterprise, branch_id
    ):
        """The operator named the period, so it applies whole. Otherwise
        asking for a period you already hold part of could not repair it."""
        dev = await make_enterprise(101)
        await seed_archive(dev, "daily", [D_OLD3])
        seen: list = []
        await self._mock_branch(mocker, branch_id, seen)

        await dpd_archive_refresh.execute_locked(D_OLD10, TODAY)

        assert self._daily(seen)[0][2] == as_dt(D_OLD10)

    async def test_without_dates_a_device_carries_on_from_its_newest_record(
        self, mocker, make_enterprise, branch_id
    ):
        dev = await make_enterprise(101)
        await seed_archive(dev, "daily", [D_OLD8, D_OLD3])
        seen: list = []
        await self._mock_branch(mocker, branch_id, seen)

        await dpd_archive_refresh.execute_locked()

        daily = self._daily(seen)
        assert len(daily) == 1
        # The newest stored day itself, not the day after it: a record of a day
        # that is still running keeps changing.
        assert daily[0][2] == as_dt(D_OLD3)
        assert daily[0][3].date() == TODAY

    async def test_without_dates_an_empty_archive_is_read_from_the_start(
        self, mocker, make_enterprise, branch_id
    ):
        await make_enterprise(101)
        seen: list = []
        await self._mock_branch(mocker, branch_id, seen)

        await dpd_archive_refresh.execute_locked()

        assert self._daily(seen)[0][2] == as_dt(archive_cleanup.FIRST_EVER)

    async def test_a_device_that_is_up_to_date_is_not_polled(
        self, mocker, make_enterprise, branch_id
    ):
        """Its newest record is today, so there is no day left to ask for."""
        dev = await make_enterprise(101)
        await seed_archive(dev, "daily", [TODAY])
        seen: list = []
        await self._mock_branch(mocker, branch_id, seen)

        await dpd_archive_refresh.execute_locked()

        # Today itself is re-read (it is still running), nothing before it.
        assert [s[2] for s in self._daily(seen)] == [as_dt(TODAY)]

    async def test_a_long_stretch_is_split_into_chunks(
        self, mocker, make_enterprise, branch_id
    ):
        """Hourly data is 24 records a day, and a cold device reaches back
        years. One answer per chunk keeps what is held in memory bounded."""
        await make_enterprise(101)
        seen: list = []
        await self._mock_branch(mocker, branch_id, seen)

        await dpd_archive_refresh.execute_locked()

        hourly = [s for s in seen if s[0] == "hourly"]
        step = dpd_archive_refresh._CHUNK_DAYS["hourly"]
        assert len(hourly) > 1
        for _, _, chunk_from, chunk_to in hourly:
            assert (chunk_to - chunk_from).days <= step + 1
        # Together they cover the whole stretch without a day in between.
        assert hourly[0][2].date() == archive_cleanup.FIRST_EVER
        assert hourly[-1][3].date() == TODAY + timedelta(days=1)

    async def test_the_future_is_not_asked_for(
        self, mocker, make_enterprise, branch_id
    ):
        """A period ending next month ends today: DPD has nothing after now,
        and asking for it only makes the request longer."""
        await make_enterprise(101)
        seen: list = []
        await self._mock_branch(mocker, branch_id, seen)

        await dpd_archive_refresh.execute_locked(D_OLD5, TODAY + timedelta(days=30))

        assert self._daily(seen)[-1][3].date() == TODAY

    async def _point_with_two_correctors(self, branch_id):
        """A point entered today whose history says one corrector stood there
        through 2024 and another took over in 2025 — the shape a point
        migrated from paper arrives in."""
        changed = datetime(2025, 1, 1)
        async with async_session_factory() as session:
            ent = Enterprise(
                enterprise_name="ent-two-correctors", active=True,
                enabled=True, branch_id=branch_id, line_id=None,
            )
            session.add(ent)
            await session.flush()
            old = DpdDevice(ser_num=7001, mf_dev=1, type_dev=3, ch_num=0)
            new = DpdDevice(ser_num=7002, mf_dev=1, type_dev=3, ch_num=0)
            session.add_all([old, new])
            await session.flush()
            session.add_all([
                EnterpriseDevice(
                    enterprise_id=ent.id, device_id=old.id,
                    installed_from=datetime(2024, 1, 1), removed_at=changed,
                ),
                EnterpriseDevice(
                    enterprise_id=ent.id, device_id=new.id,
                    installed_from=changed,
                ),
            ])
            await session.commit()
            return {"old": old.id, "new": new.id, "changed": changed}

    async def test_a_corrector_that_is_already_gone_is_still_read(
        self, mocker, branch_id
    ):
        """The point's 2024 belongs to a corrector that was taken off in 2025.
        Nothing but that corrector can supply it, so the routine run has to
        reach back past the one standing now."""
        ids = await self._point_with_two_correctors(branch_id)
        seen: list = []
        await self._mock_branch(mocker, branch_id, seen)

        await dpd_archive_refresh.execute_locked()

        asked = {s[1] for s in self._daily(seen)}
        assert asked == {ids["old"], ids["new"]}

    async def test_each_is_read_over_its_own_stretch(self, mocker, branch_id):
        """The one still installed runs to today; the one taken off stops
        where it came off, so the run does not ask DPD for a year of nothing.
        """
        ids = await self._point_with_two_correctors(branch_id)
        seen: list = []
        await self._mock_branch(mocker, branch_id, seen)

        await dpd_archive_refresh.execute_locked()

        gone = [s for s in self._daily(seen) if s[1] == ids["old"]]
        here = [s for s in self._daily(seen) if s[1] == ids["new"]]
        assert gone[0][2] == as_dt(archive_cleanup.FIRST_EVER)
        assert gone[-1][3].date() == ids["changed"].date()
        assert here[-1][3].date() == TODAY

    async def test_a_corrector_whose_archive_is_complete_is_dropped(
        self, mocker, branch_id
    ):
        """Once the archive reaches the day it came off, there is nothing left
        for it to answer — so it stops being asked, every run, forever."""
        ids = await self._point_with_two_correctors(branch_id)
        async with async_session_factory() as session:
            async with session.begin():
                await DpdArchiveDao(session).upsert_records("daily", [
                    {"device_id": ids["old"], "stamp": ids["changed"],
                     "dvst_alwrk": 1.0, "dvwrk_alwrk": None, "press": None,
                     "temper": None, "press_unit": None},
                ])
        seen: list = []
        await self._mock_branch(mocker, branch_id, seen)

        await dpd_archive_refresh.execute_locked()

        assert {s[1] for s in self._daily(seen)} == {ids["new"]}

    async def test_a_dated_reread_still_covers_the_whole_period(
        self, mocker, branch_id
    ):
        """«Перечитати архів» names the period, so it applies to both whole —
        including the one that is complete and would be skipped otherwise."""
        ids = await self._point_with_two_correctors(branch_id)
        seen: list = []
        await self._mock_branch(mocker, branch_id, seen)

        await dpd_archive_refresh.execute_locked(date(2024, 1, 1), TODAY)

        for device_id in (ids["old"], ids["new"]):
            mine = [s for s in self._daily(seen) if s[1] == device_id]
            assert mine[0][2] == as_dt(date(2024, 1, 1))
            assert mine[-1][3].date() == TODAY

    async def test_what_comes_back_is_stored(
        self, mocker, make_enterprise, branch_id
    ):
        dev = await make_enterprise(101)
        seen: list = []
        await self._mock_branch(mocker, branch_id, seen)

        await dpd_archive_refresh.execute_locked(date(2024, 1, 1), TODAY)

        rows = await archive_rows("daily")
        assert [(r["device_id"], r["day"]) for r in rows] == [
            (dev["device_id"], D_OLD5)]


class TestTheRereadEndpoint:
    async def test_it_starts_the_job(self, admin_client, mocker):
        started = mocker.patch(
            "backend.services.dpd_archive_refresh.execute_locked",
            mocker.AsyncMock(return_value=None),
        )

        resp = await admin_client.post("/enterprise/archive/reread", params={
            "from_date": "2024-01-01", "to_date": TODAY.isoformat()})

        assert resp.status_code == 202
        assert resp.json()["from_date"] == "2024-01-01"
        await asyncio.sleep(0)          # let the detached task run
        started.assert_awaited_once()

    async def test_a_backwards_period_is_refused(self, admin_client):
        resp = await admin_client.post("/enterprise/archive/reread", params={
            "from_date": "2024-06-01", "to_date": "2024-01-01"})
        assert resp.status_code == 400

    async def test_a_second_one_waits_for_the_first(self, admin_client, mocker):
        mocker.patch(
            "backend.services.dpd_archive_refresh.execute_locked",
            mocker.AsyncMock(return_value=None),
        )
        params = {"from_date": "2024-01-01", "to_date": TODAY.isoformat()}

        first = await admin_client.post("/enterprise/archive/reread", params=params)
        second = await admin_client.post("/enterprise/archive/reread", params=params)

        assert first.status_code == 202
        assert second.status_code == 409

    async def test_a_viewer_may_not_start_it(self, viewer_client):
        resp = await viewer_client.post("/enterprise/archive/reread", params={
            "from_date": "2024-01-01", "to_date": TODAY.isoformat()})
        assert resp.status_code == 403


class TestTheClockTheJobIsWrittenIn:
    """`dpd_refresh_job.started_at` is a naive column, so it has to be written
    in the clock the reader uses — the application's.

    Only the app containers carry TZ=Europe/Kyiv; Postgres runs on UTC, so
    `now()` in SQL landed three hours behind. The scheduler asks whether a slot
    has passed by comparing this value with `datetime.now()`: a run at 15:51
    was written down as 12:51, never reached the 15:00 slot, and started again
    on the next tick — every two minutes for the three hours it took UTC to
    catch up. 1121 runs in a week instead of 14 (prod logs, 21–28.09.2026),
    each of them a month-long poll of the whole branch.

    (`update_job` is not like this: its columns are `timestamptz`, where
    `now()` is an absolute instant and correct.)
    """

    async def test_it_records_when_it_actually_started(self, clean_db):
        async with async_session_factory() as session:
            await session.execute(sa_text(
                "INSERT INTO dpd_refresh_job (id, status) VALUES (1, 'idle') "
                "ON CONFLICT (id) DO UPDATE SET status = 'idle'"
            ))
            await session.commit()

        assert await dpd_archive_refresh.acquire() is True
        started = await dpd_archive_refresh.last_started_at()

        assert started is not None
        assert abs(started - datetime.now()) < timedelta(minutes=5)

    async def test_a_run_just_finished_makes_the_slot_no_longer_due(self, clean_db):
        """The comparison this feeds, end to end: a slot earlier today is not
        due again once a run has been recorded for it."""
        async with async_session_factory() as session:
            await session.execute(sa_text(
                "INSERT INTO dpd_refresh_job (id, status) VALUES (1, 'idle') "
                "ON CONFLICT (id) DO UPDATE SET status = 'idle'"
            ))
            await session.commit()
        await dpd_archive_refresh.acquire()

        now = datetime.now()
        slot = (now - timedelta(minutes=30)).strftime("%H:%M")
        due = _last_due_refresh(now, [slot])
        started = await dpd_archive_refresh.last_started_at()

        assert due is not None and started >= due


class TestRefreshSchedule:
    """The times the scheduler refreshes at are set from the admin panel and
    stored on the job row; the env value is only the default."""

    async def test_defaults_to_env_when_unset(self, clean_db):
        assert await dpd_archive_refresh.read_refresh_times() == ["10:00", "16:00"]

    async def test_put_normalizes_and_persists(self, admin_client, clean_db):
        resp = await admin_client.put(
            "/enterprise/archive/refresh/schedule",
            json={"times": ["16:00", "9:5", "16:00", "garbage"]},
        )
        assert resp.status_code == 200
        assert resp.json() == {"refresh_times": ["09:05", "16:00"]}
        assert await dpd_archive_refresh.read_refresh_times() == ["09:05", "16:00"]
        status = await admin_client.get("/enterprise/archive/refresh/status")
        assert status.json()["refresh_times"] == ["09:05", "16:00"]

    async def test_clearing_restores_the_default(self, admin_client, clean_db):
        """Choosing nothing means "use the default", not "never refresh"."""
        await admin_client.put(
            "/enterprise/archive/refresh/schedule", json={"times": ["08:00"]}
        )
        resp = await admin_client.put(
            "/enterprise/archive/refresh/schedule", json={"times": []}
        )
        assert resp.status_code == 200
        assert resp.json() == {"refresh_times": ["10:00", "16:00"]}
        assert await dpd_archive_refresh.read_refresh_times() == ["10:00", "16:00"]

    async def test_status_reports_the_default(self, admin_client, clean_db):
        body = (await admin_client.get("/enterprise/archive/refresh/status")).json()
        assert body["default_refresh_times"] == ["10:00", "16:00"]

    async def test_viewer_cannot_change_schedule(self, viewer_client, clean_db):
        resp = await viewer_client.put(
            "/enterprise/archive/refresh/schedule", json={"times": ["08:00"]}
        )
        assert resp.status_code == 403
