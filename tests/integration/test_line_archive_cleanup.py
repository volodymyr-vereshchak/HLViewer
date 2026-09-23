"""Clearing a line's archive over an open-ended range.

What it guarantees: one date on its own means everything before or after it,
two mean the span between them with both days included, all five archives go
together, and nothing belonging to another line is touched.
"""
from datetime import date, datetime

import pytest

from backend.db.engine import async_session_factory
from backend.db.models import (
    DailyArchive,
    EditArchive,
    HourlyArchive,
    Param,
    SysArchive,
)

pytestmark = pytest.mark.asyncio

_PARAM_FLOATS = {
    name: 0.0
    for name, field in Param.model_fields.items()
    if field.annotation is float
}


async def _seed(line_id: int, day: int) -> None:
    """One row of every kind on 2024-12-`day`, at a time that is not midnight:
    a day taken by its first moment alone is the mistake worth catching."""
    async with async_session_factory() as session:
        session.add_all([
            DailyArchive(period=date(2024, 12, day), volume=24000.0,
                         w_volume_dp=2.4, pressure=5.2, temperature=20.5,
                         density=0.7, line_id=line_id),
            HourlyArchive(period=datetime(2024, 12, day, 23), volume=100.0,
                          w_volume_dp=0.1, pressure=5.2, temperature=20.5,
                          density=0.7, line_id=line_id),
            EditArchive(period=datetime(2024, 12, day, 18), old_value=10,
                        new_value=20, edit_type_id=3, line_id=line_id),
            SysArchive(period=datetime(2024, 12, day, 12), sys_type_id=7,
                       volume=1.0, line_id=line_id),
            Param(**_PARAM_FLOATS, period=datetime(2024, 12, day, 6),
                  line_id=line_id),
        ])
        await session.commit()


async def _left(client, line_id: int) -> dict:
    """What the line still has, counted the way the screen counts it."""
    resp = await client.get(f"/lines/{line_id}/archive/preview")
    assert resp.status_code == 200
    return resp.json()["counts"]


class TestTheRange:
    async def test_a_start_alone_means_everything_after_it(
        self, admin_client, seed_topology
    ):
        line = seed_topology["line1"]
        for day in (10, 20, 21):
            await _seed(line, day)

        resp = await admin_client.delete(
            f"/lines/{line}/archive", params={"from_date": "2024-12-20"})

        assert resp.status_code == 200
        assert resp.json()["removed"] == {"daily": 2, "hourly": 2, "edits": 2,
                                          "alarms": 2, "params": 2}
        assert await _left(admin_client, line) == {
            "daily": 1, "hourly": 1, "edits": 1, "alarms": 1, "params": 1}

    async def test_an_end_alone_means_everything_before_it(
        self, admin_client, seed_topology
    ):
        line = seed_topology["line1"]
        for day in (10, 20, 21):
            await _seed(line, day)

        resp = await admin_client.delete(
            f"/lines/{line}/archive", params={"to_date": "2024-12-20"})

        assert resp.status_code == 200
        # The 20th goes with the 10th: the end day is cleared whole, and its
        # rows are stamped 06:00, 12:00, 18:00 and 23:00, not midnight.
        assert resp.json()["removed"]["hourly"] == 2
        assert await _left(admin_client, line) == {
            "daily": 1, "hourly": 1, "edits": 1, "alarms": 1, "params": 1}

    async def test_both_dates_mean_the_span_with_both_days_in_it(
        self, admin_client, seed_topology
    ):
        line = seed_topology["line1"]
        for day in (9, 10, 15, 20, 21):
            await _seed(line, day)

        resp = await admin_client.delete(
            f"/lines/{line}/archive",
            params={"from_date": "2024-12-10", "to_date": "2024-12-20"})

        assert resp.status_code == 200
        assert resp.json()["removed"]["daily"] == 3        # 10, 15, 20
        assert await _left(admin_client, line) == {
            "daily": 2, "hourly": 2, "edits": 2, "alarms": 2, "params": 2}

    async def test_neither_date_is_refused(self, admin_client, seed_topology):
        """Clearing a line's whole archive is not a range, and leaving both
        fields empty is the easiest thing on the screen to do by accident."""
        line = seed_topology["line1"]
        await _seed(line, 10)

        resp = await admin_client.delete(f"/lines/{line}/archive")

        assert resp.status_code == 400
        assert (await _left(admin_client, line))["daily"] == 1

    async def test_a_start_after_the_end_is_refused(
        self, admin_client, seed_topology
    ):
        resp = await admin_client.delete(
            f"/lines/{seed_topology['line1']}/archive",
            params={"from_date": "2024-12-20", "to_date": "2024-12-10"})
        assert resp.status_code == 400


class TestWhatItTouches:
    async def test_another_line_keeps_everything(self, admin_client, seed_topology):
        mine, theirs = seed_topology["line1"], seed_topology["line2"]
        await _seed(mine, 10)
        await _seed(theirs, 10)

        await admin_client.delete(f"/lines/{mine}/archive",
                                  params={"from_date": "2024-12-01"})

        assert await _left(admin_client, theirs) == {
            "daily": 1, "hourly": 1, "edits": 1, "alarms": 1, "params": 1}

    async def test_an_unknown_line_is_not_found(self, admin_client):
        resp = await admin_client.delete("/lines/424242/archive",
                                         params={"from_date": "2024-12-01"})
        assert resp.status_code == 404


class TestThePreview:
    async def test_it_counts_without_removing_anything(
        self, admin_client, seed_topology
    ):
        line = seed_topology["line1"]
        await _seed(line, 10)

        resp = await admin_client.get(f"/lines/{line}/archive/preview",
                                      params={"from_date": "2024-12-01"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["counts"] == {"daily": 1, "hourly": 1, "edits": 1,
                                  "alarms": 1, "params": 1}
        assert body["name"] == "l1"
        assert (await _left(admin_client, line))["daily"] == 1

    async def test_it_says_what_the_line_has_at_all(
        self, admin_client, seed_topology
    ):
        """Dates are picked against something real rather than a guess."""
        line = seed_topology["line1"]
        await _seed(line, 10)
        await _seed(line, 21)

        body = (await admin_client.get(f"/lines/{line}/archive/preview")).json()

        assert body["extent"] == {"first": "2024-12-10", "last": "2024-12-21"}

    async def test_an_empty_line_has_no_extent(self, admin_client, seed_topology):
        body = (await admin_client.get(
            f"/lines/{seed_topology['line2']}/archive/preview")).json()
        assert body["extent"] == {"first": None, "last": None}
        assert set(body["counts"].values()) == {0}


class TestWhoMayDoIt:
    async def test_a_viewer_may_not_clear_an_archive(
        self, viewer_client, seed_topology
    ):
        line = seed_topology["line1"]
        await _seed(line, 10)
        resp = await viewer_client.delete(f"/lines/{line}/archive",
                                        params={"from_date": "2024-12-01"})
        assert resp.status_code == 403
