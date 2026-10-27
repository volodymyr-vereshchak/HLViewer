"""Clearing a DPD line's archive over a range — one line or a whole branch.

The dates are commercial days: day D is D 07:00 up to D+1 07:00, so the
hours before 07:00 on the day after the end still belong to the last day,
and the hours before 07:00 on the first day belong to the one before it.
"""
from datetime import date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text

from backend.db.engine import async_session_factory
from backend.db.models.dpd_line_model import (
    DpdLine,
    DpdLineDailyArchive,
    DpdLineHourlyArchive,
)
from backend.db.models.grmu_branch_model import GrmuBranch

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def dpd(clean_db) -> dict:
    """Two branches; branch A has lines a1 and a2, branch B has b1."""
    ids = {}
    async with async_session_factory() as session:
        for branch_name, lines in (("A", ("a1", "a2")), ("B", ("b1",))):
            branch = GrmuBranch(name=f"Філія {branch_name}")
            session.add(branch)
            await session.flush()
            ids[branch_name] = branch.id
            for name in lines:
                line = DpdLine(name=name, branch_id=branch.id)
                session.add(line)
                await session.flush()
                ids[name] = line.id
        await session.commit()
    return ids


async def _seed(line_id: int, days=(9, 10, 11)) -> None:
    """A daily row per day, and the hours that sit on either side of 07:00."""
    async with async_session_factory() as session:
        for day in days:
            session.add(DpdLineDailyArchive(dpd_line_id=line_id, day=date(2026, 5, day),
                                            volume=100.0))
            for hour in (6, 7):
                session.add(DpdLineHourlyArchive(
                    dpd_line_id=line_id, stamp=datetime(2026, 5, day, hour), volume=1.0))
        await session.commit()


async def _left(client, line_id: int) -> dict:
    resp = await client.get(f"/dpd_lines/{line_id}/archive/preview")
    assert resp.status_code == 200
    return resp.json()["counts"]


class TestOneLine:
    async def test_the_range_is_commercial_days(self, admin_client, dpd):
        await _seed(dpd["a1"])
        resp = await admin_client.delete(
            f"/dpd_lines/{dpd['a1']}/archive",
            params={"from_date": "2026-05-10", "to_date": "2026-05-10"})
        assert resp.status_code == 200
        # day 10: the daily row, 10th 07:00 and 11th 06:00 (still day 10)
        assert resp.json()["removed"] == {"daily": 1, "hourly": 2}
        async with async_session_factory() as session:
            left = (await session.execute(text(
                "SELECT stamp FROM dpd_line_hourly_archive ORDER BY stamp"
            ))).scalars().all()
        assert [s.strftime("%d %H") for s in left] == ["09 06", "09 07", "10 06", "11 07"]

    async def test_an_open_end(self, admin_client, dpd):
        await _seed(dpd["a1"])
        resp = await admin_client.delete(
            f"/dpd_lines/{dpd['a1']}/archive", params={"from_date": "2026-05-10"})
        assert resp.json()["removed"] == {"daily": 2, "hourly": 3}

    async def test_other_lines_keep_everything(self, admin_client, dpd):
        await _seed(dpd["a1"])
        await _seed(dpd["a2"])
        await admin_client.delete(
            f"/dpd_lines/{dpd['a1']}/archive", params={"to_date": "2026-05-31"})
        assert await _left(admin_client, dpd["a1"]) == {"daily": 0, "hourly": 0}
        assert await _left(admin_client, dpd["a2"]) == {"daily": 3, "hourly": 6}

    async def test_preview_counts_and_extent(self, admin_client, dpd):
        await _seed(dpd["a1"])
        body = (await admin_client.get(
            f"/dpd_lines/{dpd['a1']}/archive/preview",
            params={"to_date": "2026-05-09"})).json()
        # up to 10th 07:00, not including it: 09 06, 09 07 and 10 06
        assert body["counts"] == {"daily": 1, "hourly": 3}
        # 09 06:00 is commercial day 8
        assert body["extent"] == {"first": "2026-05-08", "last": "2026-05-11"}
        assert await _left(admin_client, dpd["a1"]) == {"daily": 3, "hourly": 6}

    async def test_neither_date_means_the_whole_archive(self, admin_client, dpd):
        await _seed(dpd["a1"])
        resp = await admin_client.delete(f"/dpd_lines/{dpd['a1']}/archive")
        assert resp.status_code == 200
        assert resp.json()["removed"] == {"daily": 3, "hourly": 6}

    async def test_a_start_after_the_end_is_refused(self, admin_client, dpd):
        resp = await admin_client.delete(
            f"/dpd_lines/{dpd['a1']}/archive",
            params={"from_date": "2026-05-10", "to_date": "2026-05-09"})
        assert resp.status_code == 400

    async def test_refused_while_the_line_is_updating(self, admin_client, dpd):
        await _seed(dpd["a1"])
        async with async_session_factory() as session:
            await session.execute(text(
                "INSERT INTO dpd_line_job (dpd_line_id, kind, status, updated_at) "
                "VALUES (:id, 'init', 'running', now())"), {"id": dpd["a1"]})
            await session.commit()
        resp = await admin_client.delete(
            f"/dpd_lines/{dpd['a1']}/archive", params={"from_date": "2026-05-01"})
        assert resp.status_code == 409
        assert "a1" in resp.json()["detail"]
        assert (await _left(admin_client, dpd["a1"]))["daily"] == 3

    async def test_unknown_line(self, admin_client):
        resp = await admin_client.get("/dpd_lines/999999/archive/preview")
        assert resp.status_code == 404


class TestManyLines:
    async def test_no_branch_means_every_dpd_line(self, admin_client, dpd):
        for name in ("a1", "a2", "b1"):
            await _seed(dpd[name])
        preview = (await admin_client.get(
            "/dpd_lines/archive/bulk/preview", params={"from_date": "2026-05-11"})).json()
        assert preview["lines"] == 3
        resp = await admin_client.delete(
            "/dpd_lines/archive/bulk", params={"from_date": "2026-05-11"})
        assert resp.json()["removed"] == {"daily": 3, "hourly": 3}
        for name in ("a1", "a2", "b1"):
            assert await _left(admin_client, dpd[name]) == {"daily": 2, "hourly": 5}

    async def test_every_dpd_line_of_the_branch_and_nothing_else(self, admin_client, dpd):
        for name in ("a1", "a2", "b1"):
            await _seed(dpd[name])
        preview = (await admin_client.get(
            "/dpd_lines/archive/bulk/preview",
            params={"branch_id": dpd["A"], "from_date": "2026-05-10"})).json()
        assert preview["lines"] == 2
        assert preview["counts"] == {"daily": 4, "hourly": 6}

        resp = await admin_client.delete(
            "/dpd_lines/archive/bulk", params={"branch_id": dpd["A"], "from_date": "2026-05-10"})
        assert resp.status_code == 200
        assert resp.json()["removed"] == {"daily": 4, "hourly": 6}
        assert await _left(admin_client, dpd["a2"]) == {"daily": 1, "hourly": 3}
        assert await _left(admin_client, dpd["b1"]) == {"daily": 3, "hourly": 6}

    async def test_refused_while_any_line_of_it_is_updating(self, admin_client, dpd):
        await _seed(dpd["a1"])
        async with async_session_factory() as session:
            await session.execute(text(
                "INSERT INTO dpd_line_job (dpd_line_id, kind, status, updated_at) "
                "VALUES (:id, 'update', 'running', now())"), {"id": dpd["a2"]})
            await session.commit()
        resp = await admin_client.delete(
            "/dpd_lines/archive/bulk", params={"branch_id": dpd["A"], "from_date": "2026-05-01"})
        assert resp.status_code == 409
        assert "a2" in resp.json()["detail"]

    async def test_a_dead_job_does_not_block(self, admin_client, dpd):
        await _seed(dpd["a1"])
        async with async_session_factory() as session:
            await session.execute(text(
                "INSERT INTO dpd_line_job (dpd_line_id, kind, status, updated_at) "
                "VALUES (:id, 'init', 'running', now() - interval '2 hours')"),
                {"id": dpd["a1"]})
            await session.commit()
        resp = await admin_client.delete(
            "/dpd_lines/archive/bulk", params={"branch_id": dpd["A"], "from_date": "2026-05-01"})
        assert resp.status_code == 200
