"""Archive endpoints of DPD lines (/daily_dpd/, /hourly_dpd/).

A DPD line carries no unit configuration — pressure arrives from the API with
the unit the device reported, so the archive rows must hand that unit to the
client instead of leaving it to guess."""

from datetime import date, datetime, timedelta

import pytest_asyncio

from backend.db.engine import async_session_factory
from backend.db.models.dpd_line_model import (
    DpdLine,
    DpdLineDailyArchive,
    DpdLineHourlyArchive,
)
from backend.db.models.grmu_branch_model import GrmuBranch

DAY = date(2026, 5, 3)


@pytest_asyncio.fixture
async def dpd_line(clean_db) -> int:
    async with async_session_factory() as session:
        branch = GrmuBranch(name="Філія ДПД")
        session.add(branch)
        await session.flush()
        line = DpdLine(name="ДПД лінія", branch_id=branch.id)
        session.add(line)
        await session.flush()
        session.add_all([
            DpdLineDailyArchive(
                dpd_line_id=line.id, day=DAY, volume=100.0,
                pressure=3.2, temperature=15.0, press_unit=" кПа ",
            ),
            DpdLineHourlyArchive(
                dpd_line_id=line.id, stamp=datetime(2026, 5, 3, 10),
                volume=10.0, pressure=3.3, temperature=15.5, press_unit="None",
            ),
        ])
        line_id = line.id
        await session.commit()
    return line_id


class TestDpdLineArchiveUnits:
    async def test_daily_reports_the_device_unit(self, admin_client, dpd_line):
        resp = await admin_client.get("/daily_dpd/", params={
            "line_id": [dpd_line],
            "from_date": "2026-05-01T00:00:00",
            "to_date": "2026-05-05T00:00:00",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["press_unit"] == "кПа"  # trimmed, straight from DPD
        assert body[0]["pressure"] == 3.2

    async def test_hourly_absent_unit_is_null(self, admin_client, dpd_line):
        """The literal "None" some correctors send is not a unit."""
        resp = await admin_client.get("/hourly_dpd/", params={
            "line_id": [dpd_line],
            "from_date": "2026-05-03T00:00:00",
            "to_date": "2026-05-03T23:00:00",
        })
        assert resp.status_code == 200
        assert resp.json()[0]["press_unit"] is None

    async def test_range_cap_still_applies(self, admin_client, dpd_line):
        start = datetime(2026, 1, 1)
        resp = await admin_client.get("/daily_dpd/", params={
            "line_id": [dpd_line],
            "from_date": start.isoformat(),
            "to_date": (start + timedelta(days=401)).isoformat(),
        })
        assert resp.status_code == 400
