"""Archive endpoints of DPD lines (/daily_dpd/, /hourly_dpd/).

Every row is STORED in the unit its corrector reported — correctors of one
line report different ones — and READ in the unit the line is set to show
(`dpd_line.pressure_unit`). Converting once on the way out is what lets the
overview, the reports and the rings all print the same number under the same
caption; before, each reader was left to convert per row, and most did not."""

from datetime import date, datetime, timedelta

import pytest
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
    async def test_a_row_is_read_in_the_lines_unit(self, admin_client, dpd_line):
        """Stored as the corrector said it (кПа), shown as the line is set
        (кгс/см², the default)."""
        resp = await admin_client.get("/daily_dpd/", params={
            "line_id": [dpd_line],
            "from_date": "2026-05-01T00:00:00",
            "to_date": "2026-05-05T00:00:00",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["press_unit"] == "кгс/см²"
        assert body[0]["pressure"] == pytest.approx(3.2 * 1000 / 98066.5)

    async def test_the_lines_setting_is_what_decides(self, admin_client, dpd_line):
        async with async_session_factory() as session:
            line = await session.get(DpdLine, dpd_line)
            line.pressure_unit = "кПа"
            await session.commit()
        body = (await admin_client.get("/daily_dpd/", params={
            "line_id": [dpd_line],
            "from_date": "2026-05-01T00:00:00",
            "to_date": "2026-05-05T00:00:00",
        })).json()
        assert (body[0]["pressure"], body[0]["press_unit"]) == (3.2, "кПа")

    async def test_a_row_with_no_unit_is_taken_as_the_lines(
        self, admin_client, dpd_line
    ):
        """The literal "None" some correctors send is not a unit — and with no
        other row to go by, the number is left as the line's."""
        resp = await admin_client.get("/hourly_dpd/", params={
            "line_id": [dpd_line],
            "from_date": "2026-05-03T00:00:00",
            "to_date": "2026-05-03T23:00:00",
        })
        assert resp.status_code == 200
        row = resp.json()[0]
        assert (row["pressure"], row["press_unit"]) == (3.3, "кгс/см²")

    async def test_one_line_reading_two_units_reads_as_one(
        self, admin_client, dpd_line
    ):
        """A corrector replaced mid-history, the new one reporting in МПа: the
        same pressure must not print as 3.3 and then 0.32 in adjacent rows."""
        async with async_session_factory() as session:
            session.add_all([
                DpdLineHourlyArchive(
                    dpd_line_id=dpd_line, stamp=datetime(2026, 5, 3, 11),
                    volume=10.0, pressure=3.3, temperature=15.5,
                    press_unit="кгс/см3",          # the API's spelling of кгс/см²
                ),
                DpdLineHourlyArchive(
                    dpd_line_id=dpd_line, stamp=datetime(2026, 5, 3, 12),
                    volume=10.0, pressure=0.3236, temperature=15.5,
                    press_unit="МПа",
                ),
            ])
            await session.commit()
        rows = (await admin_client.get("/hourly_dpd/", params={
            "line_id": [dpd_line],
            "from_date": "2026-05-03T11:00:00",
            "to_date": "2026-05-03T12:00:00",
        })).json()
        assert [r["press_unit"] for r in rows] == ["кгс/см²", "кгс/см²"]
        assert rows[0]["pressure"] == pytest.approx(3.3)
        assert rows[1]["pressure"] == pytest.approx(3.3, abs=0.001)

    async def test_no_range_is_too_long(self, admin_client, dpd_line):
        """Used to stop at 400 days (daily) and 90 (hourly)."""
        start = datetime(2026, 1, 1)
        for path in ("/daily_dpd/", "/hourly_dpd/"):
            resp = await admin_client.get(path, params={
                "line_id": [dpd_line],
                "from_date": start.isoformat(),
                "to_date": (start + timedelta(days=3000)).isoformat(),
            })
            assert resp.status_code == 200

    async def test_dates_are_still_required(self, admin_client, dpd_line):
        resp = await admin_client.get("/daily_dpd/", params={"line_id": [dpd_line]})
        assert resp.status_code == 400


class TestDpdLineInCompactArchive:
    """DPD lines come back through /hourly_compact/ too — the night report
    reads all three kinds of line from that one endpoint."""

    async def test_hourly_compact_serves_dpd_lines(self, admin_client, dpd_line):
        resp = await admin_client.get("/hourly_compact/", params={
            "line_id": [dpd_line],
            "from_date": "2026-05-03T00:00:00",
            "to_date": "2026-05-03T23:00:00",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["stamps"] == ["2026-05-03T10"]
        assert body["rows"] == [[dpd_line, 0, 10.0]]

    async def test_hourly_compact_honours_the_hours_filter(self, admin_client, dpd_line):
        resp = await admin_client.get("/hourly_compact/", params={
            "line_id": [dpd_line],
            "from_date": "2026-05-03T00:00:00",
            "to_date": "2026-05-03T23:00:00",
            "hours": [2, 3],
        })
        assert resp.json() == {"stamps": [], "rows": []}
