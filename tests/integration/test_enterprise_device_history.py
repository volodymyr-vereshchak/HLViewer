"""Corrector history: what a metering point reads when its device changed.

The archive is keyed by the CORRECTOR, and a point reads slices of it through
its assignment windows. These tests pin the consequences that motivated the
design: a moved corrector never carries its gas to the point it left, a gap
between removal and the next install belongs to nobody, and correcting a date
re-slices what is already stored instead of re-asking DPD.

DPDClient is mocked, Postgres is real."""

from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text

from backend.db.dao.dpd_archive_dao import DpdArchiveDao
from backend.db.engine import async_session_factory
from backend.db.models.enterprise_model import (
    DpdDevice, Enterprise, EnterpriseDevice, EPOCH_INSTALLED_FROM,
)
from backend.db.models.grmu_branch_model import GrmuBranch
from backend.services.enterprise_mappings import get_devices_for_lines_db
from backend.services.enterprise_volume_service import (
    aggregate_volumes, fetch_dpd_volumes,
)

MARCH = 2026


def d(day: int) -> date:
    return date(MARCH, 3, day)


def dt(day: int, hour: int = 0) -> datetime:
    return datetime(MARCH, 3, day, hour)


@pytest_asyncio.fixture
async def branch_id(clean_db) -> int:
    async with async_session_factory() as session:
        branch = GrmuBranch(name="Тестова філія")
        session.add(branch)
        await session.commit()
        return branch.id


@pytest_asyncio.fixture
async def topology(branch_id):
    """Two metering points on one line, and two correctors to move between
    them. Everything is built directly so a test can state a history in one
    call and read the consequence."""
    state = {"branch_id": branch_id, "line_id": 1}

    async def make_point(name: str) -> int:
        async with async_session_factory() as session:
            ent = Enterprise(
                enterprise_name=name, branch_id=branch_id, line_id=None,
                active=True, enabled=True,
            )
            session.add(ent)
            await session.commit()
            await session.refresh(ent)
            return ent.id

    async def make_device(ser_num: int) -> int:
        async with async_session_factory() as session:
            device = DpdDevice(ser_num=ser_num, mf_dev=1, type_dev=3, ch_num=0)
            session.add(device)
            await session.commit()
            await session.refresh(device)
            return device.id

    async def assign(point_id: int, device_id: int, installed_from, removed_at=None):
        async with async_session_factory() as session:
            session.add(EnterpriseDevice(
                enterprise_id=point_id, device_id=device_id,
                installed_from=installed_from, removed_at=removed_at,
            ))
            await session.commit()

    async def seed_days(device_id: int, days: list[date], volume: float):
        """The corrector's own archive — it exists regardless of where the
        device stood, which is the whole point of keying by device."""
        async with async_session_factory() as session:
            async with session.begin():
                dao = DpdArchiveDao(session)
                await dao.upsert_records("daily", [
                    {"device_id": device_id,
                     "stamp": datetime.combine(day, datetime.min.time()),
                     "dvst_alwrk": volume, "dvwrk_alwrk": None,
                     "press": None, "temper": None, "press_unit": None}
                    for day in days
                ])
                # Fully covered: no read may reach the DPD API.
                await dao.lower_loaded_from([device_id], "daily", d(1))

    async def seed_hours(device_id: int, stamps: list[datetime], volume: float):
        async with async_session_factory() as session:
            async with session.begin():
                dao = DpdArchiveDao(session)
                await dao.upsert_records("hourly", [
                    {"device_id": device_id, "stamp": s,
                     "dvst_alwrk": volume, "dvwrk_alwrk": None,
                     "press": None, "temper": None, "press_unit": None}
                    for s in stamps
                ])
                await dao.lower_loaded_from([device_id], "hourly", d(1))

    async def clear_history(point_id: int):
        async with async_session_factory() as session:
            await session.execute(
                text("DELETE FROM enterprise_device WHERE enterprise_id = :p"),
                {"p": point_id},
            )
            await session.commit()

    state.update(
        make_point=make_point, make_device=make_device, assign=assign,
        seed_days=seed_days, seed_hours=seed_hours, clear_history=clear_history,
    )
    return state


@pytest.fixture
def dpd_mock(mocker):
    client = mocker.AsyncMock()
    mocker.patch(
        "backend.services.enterprise_volume_service.DPDClient.for_branch",
        mocker.AsyncMock(return_value=client),
    )
    return client


async def read_days(topology, point_ids, period_from, period_to) -> dict:
    """{point name: {day: volume}} the way an endpoint would report it."""
    async with async_session_factory() as session:
        # Points are linked to no line here, so they are addressed directly.
        assignments = await _assignments_for(session, point_ids, period_from, period_to)
    records = await fetch_dpd_volumes(
        assignments, period_from, period_to, "daily"
    )
    result = aggregate_volumes(records, assignments, "daily")
    by_point: dict = {}
    for record in records:
        assignment = next(a for a in assignments if a["assignment_id"] == record["tag"])
        by_point.setdefault(assignment["enterprise_name"], {})[
            record["date"][:10]
        ] = record["dvstAlwrk"]
    return by_point, result


async def _assignments_for(session, point_ids, range_from, range_to):
    from backend.services.enterprise_mappings import _query_assignments_db
    return await _query_assignments_db(
        session, Enterprise.id.in_(point_ids),
        range_from=range_from, range_to=range_to,
    )


class TestMovedCorrector:
    async def test_each_point_reads_only_its_own_stretch(self, dpd_mock, topology):
        """#7 measures point A until 10 March, then point B. Neither may see
        the other's days — before the split the whole archive belonged to
        whichever point the row happened to name."""
        a = await topology["make_point"]("Точка А")
        b = await topology["make_point"]("Точка Б")
        device = await topology["make_device"](7)
        await topology["assign"](a, device, EPOCH_INSTALLED_FROM, removed_at=dt(10, 7))
        await topology["assign"](b, device, dt(10, 7))
        await topology["seed_days"](device, [d(8), d(9), d(10), d(11)], 5.0)

        by_point, _ = await read_days(topology, [a, b], dt(1), dt(20))

        assert sorted(by_point["Точка А"]) == ["2026-03-08", "2026-03-09"]
        assert sorted(by_point["Точка Б"]) == ["2026-03-10", "2026-03-11"]
        dpd_mock.get_volumes.assert_not_awaited()

    async def test_shared_corrector_is_polled_once(self, dpd_mock, topology):
        """Coverage is per device, so the second point reads what the first
        already pulled instead of asking DPD again."""
        a = await topology["make_point"]("Точка А")
        b = await topology["make_point"]("Точка Б")
        device = await topology["make_device"](7)
        await topology["assign"](a, device, EPOCH_INSTALLED_FROM, removed_at=dt(10, 7))
        await topology["assign"](b, device, dt(10, 7))
        dpd_mock.get_volumes.return_value = []

        async with async_session_factory() as session:
            assignments = await _assignments_for(session, [a, b], dt(1), dt(20))
        await fetch_dpd_volumes(assignments, dt(1), dt(20), "daily")

        # Two assignments, one device → one poll, and its coverage now spans
        # the range for both points.
        assert dpd_mock.get_volumes.await_count == 1
        async with async_session_factory() as session:
            rows = (await session.execute(
                text("SELECT device_id FROM dpd_device_coverage "
                     "WHERE period_type = 'daily'")
            )).scalars().all()
        assert rows == [device]


class TestGap:
    async def test_days_without_a_device_belong_to_nobody(self, dpd_mock, topology):
        """Taken off on the 5th, replaced on the 10th. The old corrector is
        already measuring somewhere else in between, so those days must be
        empty rather than carry its readings."""
        point = await topology["make_point"]("Точка А")
        old = await topology["make_device"](7)
        new = await topology["make_device"](8)
        await topology["assign"](point, old, EPOCH_INSTALLED_FROM, removed_at=dt(5, 7))
        await topology["assign"](point, new, dt(10, 7))
        # The old corrector keeps producing data — at its new home.
        await topology["seed_days"](old, [d(4), d(5), d(6), d(7), d(8), d(9)], 5.0)
        await topology["seed_days"](new, [d(10), d(11)], 9.0)

        by_point, _ = await read_days(topology, [point], dt(1), dt(20))

        assert sorted(by_point["Точка А"]) == [
            "2026-03-04", "2026-03-10", "2026-03-11",
        ]
        # 5–9 March produced nothing at all — no zeros invented, no old data.
        assert "2026-03-05" not in by_point["Точка А"]
        assert "2026-03-09" not in by_point["Точка А"]

    async def test_gap_lowers_the_line_total_for_those_days(self, dpd_mock, topology):
        """The aggregate simply has no entry for a gap day, so a report that
        sums the line finds no industrial volume there."""
        point = await topology["make_point"]("Точка А")
        device = await topology["make_device"](7)
        await topology["assign"](point, device, EPOCH_INSTALLED_FROM, removed_at=dt(5, 7))
        await topology["seed_days"](device, [d(4), d(6)], 5.0)

        _, result = await read_days(topology, [point], dt(1), dt(20))

        periods = {str(r.period) for r in result}
        assert "2026-03-04" in periods
        assert "2026-03-06" not in periods


class TestHourlyStitching:
    async def test_replacement_at_14_00_splits_on_the_hour(self, dpd_mock, topology):
        """The reason install moments carry an hour: a same-day replacement
        must split the hourly archive exactly where it happened, and the daily
        archive cannot show that error."""
        point = await topology["make_point"]("Точка А")
        old = await topology["make_device"](7)
        new = await topology["make_device"](8)
        await topology["assign"](point, old, EPOCH_INSTALLED_FROM)
        await topology["assign"](point, new, dt(10, 14))
        day_hours = [dt(10, h) for h in range(24)]
        await topology["seed_hours"](old, day_hours, 1.0)
        await topology["seed_hours"](new, day_hours, 2.0)

        async with async_session_factory() as session:
            assignments = await _assignments_for(
                session, [point], dt(10, 7), dt(11, 6)
            )
        records = await fetch_dpd_volumes(assignments, dt(10), dt(10), "hourly")

        by_hour = {
            datetime.fromisoformat(r["date"]).hour: r["dvstAlwrk"] for r in records
        }
        # Up to 13:00 the old corrector, from 14:00 the new one.
        assert by_hour[13] == 1.0
        assert by_hour[14] == 2.0
        # And no hour is counted twice.
        assert len(records) == len(by_hour)


class TestMidDayReplacementDaily:
    async def test_changeover_day_belongs_to_one_device_only(self, dpd_mock, topology):
        """A corrector fitted at 14:00 does not own that commercial day — it
        opened at 07:00 under the previous one.

        The daily record covers 07:00→07:00, so handing it to both devices
        would double the point's volume for the day. The hourly archive splits
        on the hour and cannot show this, which is why it needs its own test.
        """
        point = await topology["make_point"]("Точка А")
        old = await topology["make_device"](7)
        new = await topology["make_device"](8)
        await topology["assign"](point, old, EPOCH_INSTALLED_FROM)
        await topology["assign"](point, new, dt(10, 14))
        days = [d(9), d(10), d(11)]
        await topology["seed_days"](old, days, 5.0)
        await topology["seed_days"](new, days, 9.0)

        by_point, result = await read_days(topology, [point], dt(1), dt(20))

        # The 10th opened under the old corrector, the 11th under the new.
        assert by_point["Точка А"]["2026-03-09"] == 5.0
        assert by_point["Точка А"]["2026-03-10"] == 5.0
        assert by_point["Точка А"]["2026-03-11"] == 9.0
        # And no day is reported twice.
        totals = {str(r.period): r.total_volume for r in result}
        assert totals["2026-03-10"] == 5.0
        assert all(r.device_count == 1 for r in result)


class TestHistoryCorrection:
    async def test_moving_the_date_reslices_without_polling(self, dpd_mock, topology):
        """The payoff of keying the archive by device: fixing a wrong install
        date changes what the point shows and costs no DPD traffic at all."""
        point = await topology["make_point"]("Точка А")
        old = await topology["make_device"](7)
        new = await topology["make_device"](8)
        await topology["assign"](point, old, EPOCH_INSTALLED_FROM)
        await topology["assign"](point, new, dt(10, 7))
        await topology["seed_days"](old, [d(8), d(9), d(10), d(11)], 5.0)
        await topology["seed_days"](new, [d(8), d(9), d(10), d(11)], 9.0)

        before, _ = await read_days(topology, [point], dt(1), dt(20))
        assert before["Точка А"]["2026-03-09"] == 5.0
        assert before["Точка А"]["2026-03-10"] == 9.0

        # The replacement actually happened on the 9th, not the 10th.
        await topology["clear_history"](point)
        await topology["assign"](point, old, EPOCH_INSTALLED_FROM)
        await topology["assign"](point, new, dt(9, 7))

        after, _ = await read_days(topology, [point], dt(1), dt(20))
        assert after["Точка А"]["2026-03-09"] == 9.0
        assert after["Точка А"]["2026-03-08"] == 5.0
        dpd_mock.get_volumes.assert_not_awaited()


class TestHistoryValidation:
    async def test_overlapping_windows_rejected(self, admin_client, seed_topology):
        resp = await admin_client.post(
            "/enterprise-mappings/",
            json={
                "enterprise_name": "Точка А",
                "branch_id": seed_topology["branch"],
                "line_id": seed_topology["line1"],
                "devices": [
                    {"ser_num": 7, "ch_num": 0, "mf_dev": 1, "type_dev": 3,
                     "installed_from": "2026-03-01T07:00:00"},
                    {"ser_num": 8, "ch_num": 0, "mf_dev": 1, "type_dev": 3,
                     "installed_from": "2026-03-01T07:00:00"},
                ],
            },
        )
        assert resp.status_code == 400
        assert "однакову дату" in resp.json()["detail"]

    async def test_same_device_at_two_points_at_once_rejected(
        self, admin_client, seed_topology
    ):
        """One corrector cannot stand at two metering points at the same time:
        allowing it would count the same gas twice on the line."""
        device = {"ser_num": 7, "ch_num": 0, "mf_dev": 1, "type_dev": 3,
                  "installed_from": "2026-03-01T07:00:00"}
        first = await admin_client.post(
            "/enterprise-mappings/",
            json={
                "enterprise_name": "Точка А",
                "branch_id": seed_topology["branch"],
                "line_id": seed_topology["line1"],
                "devices": [device],
            },
        )
        assert first.status_code == 201

        second = await admin_client.post(
            "/enterprise-mappings/",
            json={
                "enterprise_name": "Точка Б",
                "branch_id": seed_topology["branch"],
                "line_id": seed_topology["line1"],
                "devices": [dict(device, installed_from="2026-03-05T07:00:00")],
            },
        )
        assert second.status_code == 400, second.text
        assert "Точка А" in second.json()["detail"]

    async def test_moving_a_corrector_with_a_removal_date_is_allowed(
        self, admin_client, seed_topology
    ):
        """The same move, once the old point says when the device left."""
        first = await admin_client.post(
            "/enterprise-mappings/",
            json={
                "enterprise_name": "Точка А",
                "branch_id": seed_topology["branch"],
                "line_id": seed_topology["line1"],
                "devices": [{
                    "ser_num": 7, "ch_num": 0, "mf_dev": 1, "type_dev": 3,
                    "installed_from": "2026-03-01T07:00:00",
                    "removed_at": "2026-03-05T07:00:00",
                }],
            },
        )
        assert first.status_code == 201

        second = await admin_client.post(
            "/enterprise-mappings/",
            json={
                "enterprise_name": "Точка Б",
                "branch_id": seed_topology["branch"],
                "line_id": seed_topology["line1"],
                "devices": [{
                    "ser_num": 7, "ch_num": 0, "mf_dev": 1, "type_dev": 3,
                    "installed_from": "2026-03-05T07:00:00",
                }],
            },
        )
        assert second.status_code == 201, second.text

    async def test_minutes_are_floored_to_the_hour(self, admin_client, seed_topology):
        """DPD's hourly records land on the hour; anything finer could not be
        lined up with them."""
        resp = await admin_client.post(
            "/enterprise-mappings/",
            json={
                "enterprise_name": "Точка А",
                "branch_id": seed_topology["branch"],
                "line_id": seed_topology["line1"],
                "devices": [{
                    "ser_num": 7, "ch_num": 0, "mf_dev": 1, "type_dev": 3,
                    "installed_from": "2026-03-01T14:37:29",
                }],
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["devices"][0]["installed_from"] == "2026-03-01T14:00:00"

    async def test_patch_without_devices_leaves_history_alone(
        self, admin_client, seed_topology
    ):
        """Toggling a flag must not rewrite when correctors stood where."""
        created = await admin_client.post(
            "/enterprise-mappings/",
            json={
                "enterprise_name": "Точка А",
                "branch_id": seed_topology["branch"],
                "line_id": seed_topology["line1"],
                "devices": [{
                    "ser_num": 7, "ch_num": 0, "mf_dev": 1, "type_dev": 3,
                    "installed_from": "2026-03-01T07:00:00",
                }],
            },
        )
        ent_id = created.json()["id"]

        patched = await admin_client.patch(
            f"/enterprise-mappings/{ent_id}", json={"enabled": False}
        )
        assert patched.status_code == 200
        assert len(patched.json()["devices"]) == 1
        assert patched.json()["devices"][0]["ser_num"] == 7


class TestLineResolution:
    async def test_only_windows_overlapping_the_range_are_returned(
        self, seed_topology
    ):
        """A corrector removed before the requested period is not polled and
        not read — it has nothing to say about those days."""
        line_id = seed_topology["line1"]
        async with async_session_factory() as session:
            ent = Enterprise(
                enterprise_name="Точка А", branch_id=seed_topology["branch"],
                line_id=line_id, active=True, enabled=True,
            )
            session.add(ent)
            await session.flush()
            old = DpdDevice(ser_num=7, mf_dev=1, type_dev=3, ch_num=0)
            new = DpdDevice(ser_num=8, mf_dev=1, type_dev=3, ch_num=0)
            session.add_all([old, new])
            await session.flush()
            session.add_all([
                EnterpriseDevice(enterprise_id=ent.id, device_id=old.id,
                                 installed_from=EPOCH_INSTALLED_FROM,
                                 removed_at=dt(5, 7)),
                EnterpriseDevice(enterprise_id=ent.id, device_id=new.id,
                                 installed_from=dt(10, 7)),
            ])
            await session.commit()

            after = await get_devices_for_lines_db(
                [line_id], session, range_from=dt(12), range_to=dt(20),
            )
            assert [a["serNum"] for a in after] == [8]

            during_gap = await get_devices_for_lines_db(
                [line_id], session, range_from=dt(6), range_to=dt(9),
            )
            assert during_gap == []
