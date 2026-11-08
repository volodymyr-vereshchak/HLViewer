"""Analytics weather: points and their lines, import (upsert, nulls, DST
duplicates), the download plan, reads and branch scoping."""
from datetime import date, datetime, timedelta

from backend.db.engine import async_session_factory
from backend.db.models import DailyArchive


async def _point(client, branch_id, name="ГРС Тест", lines=(), lat=47.84, lon=35.17):
    resp = await client.post("/weather/points", json={
        "branch_id": branch_id, "name": name, "latitude": lat, "longitude": lon,
        "lines": [{"kind": "physical", "id": i} for i in lines],
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _archive(line_id: int, days: list[date]) -> None:
    async with async_session_factory() as session:
        for d in days:
            session.add(DailyArchive(
                line_id=line_id, period=d, volume=1, w_volume_dp=1,
                pressure=1, temperature=1, density=0.7,
            ))
        await session.commit()


class TestPoints:
    async def test_create_lists_lines_and_empty_coverage(self, admin_client, seed_topology):
        p = await _point(admin_client, seed_topology["branch"], lines=[seed_topology["line1"]])
        assert p["lines"] == [{"kind": "physical", "id": seed_topology["line1"]}]
        assert p["daily"] == {"first": None, "last": None, "count": 0}

        listed = (await admin_client.get(
            "/weather/points", params={"branch_id": seed_topology["branch"]})).json()
        assert [x["id"] for x in listed] == [p["id"]]

    async def test_duplicate_name_in_branch_is_409(self, admin_client, seed_topology):
        await _point(admin_client, seed_topology["branch"])
        resp = await admin_client.post("/weather/points", json={
            "branch_id": seed_topology["branch"], "name": "ГРС Тест",
            "latitude": 1, "longitude": 1,
        })
        assert resp.status_code == 409

    async def test_a_line_moves_to_the_point_that_takes_it(self, admin_client, seed_topology):
        b, l1 = seed_topology["branch"], seed_topology["line1"]
        a = await _point(admin_client, b, "A", lines=[l1])
        c = await _point(admin_client, b, "B", lines=[l1])
        listed = {x["id"]: x for x in (await admin_client.get(
            "/weather/points", params={"branch_id": b})).json()}
        assert listed[a["id"]]["lines"] == []
        assert listed[c["id"]]["lines"] == [{"kind": "physical", "id": l1}]

    async def test_patch_replaces_lines_only_when_given(self, admin_client, seed_topology):
        p = await _point(admin_client, seed_topology["branch"], lines=[seed_topology["line1"]])
        moved = (await admin_client.patch(
            f"/weather/points/{p['id']}", json={"latitude": 48.0})).json()
        assert moved["latitude"] == 48.0
        assert len(moved["lines"]) == 1
        cleared = (await admin_client.patch(
            f"/weather/points/{p['id']}", json={"lines": []})).json()
        assert cleared["lines"] == []

    async def test_unknown_line_kind_is_rejected(self, admin_client, seed_topology):
        resp = await admin_client.post("/weather/points", json={
            "branch_id": seed_topology["branch"], "name": "X", "latitude": 1, "longitude": 1,
            "lines": [{"kind": "bogus", "id": 1}],
        })
        assert resp.status_code == 422

    async def test_viewer_cannot_write(self, viewer_client, seed_topology):
        resp = await viewer_client.post("/weather/points", json={
            "branch_id": seed_topology["branch"], "name": "X", "latitude": 1, "longitude": 1,
        })
        assert resp.status_code == 403

    async def test_delete_takes_the_rows_with_it(self, admin_client, seed_topology):
        p = await _point(admin_client, seed_topology["branch"])
        await admin_client.post("/weather/import", json={
            "point_id": p["id"], "daily": [{"day": "2026-01-01", "t_mean": -3}],
        })
        assert (await admin_client.delete(f"/weather/points/{p['id']}")).status_code == 204
        assert (await admin_client.get("/weather/points")).json() == []


class TestImport:
    async def test_upsert_overwrites_and_drops_empty_rows(self, admin_client, seed_topology):
        p = await _point(admin_client, seed_topology["branch"])
        first = (await admin_client.post("/weather/import", json={
            "point_id": p["id"],
            "daily": [
                {"day": "2026-01-01", "t_mean": -3, "t_min": -6, "t_max": 0},
                {"day": "2026-01-02"},  # reanalysis not there yet: all nulls
            ],
        })).json()
        assert first == {"daily": 1, "hourly": 0}
        await admin_client.post("/weather/import", json={
            "point_id": p["id"], "daily": [{"day": "2026-01-01", "t_mean": -4}],
        })
        rows = (await admin_client.get("/weather/daily", params={
            "point_id": p["id"], "date_from": "2026-01-01", "date_to": "2026-01-31",
        })).json()
        assert len(rows) == 1
        assert rows[0]["t_mean"] == -4
        assert rows[0]["t_min"] is None  # the second import said nothing about it

    async def test_a_repeated_hour_keeps_the_last(self, admin_client, seed_topology):
        p = await _point(admin_client, seed_topology["branch"])
        res = (await admin_client.post("/weather/import", json={
            "point_id": p["id"],
            "hourly": [
                {"stamp": "2026-10-25T03:00:00", "temperature": 5},
                {"stamp": "2026-10-25T03:00:00", "temperature": 4},
            ],
        })).json()
        assert res["hourly"] == 1
        rows = (await admin_client.get("/weather/hourly", params={
            "point_id": p["id"], "date_from": "2026-10-25", "date_to": "2026-10-25",
        })).json()
        assert [r["temperature"] for r in rows] == [4]

    async def test_coverage_shows_in_the_point(self, admin_client, seed_topology):
        p = await _point(admin_client, seed_topology["branch"])
        await admin_client.post("/weather/import", json={
            "point_id": p["id"],
            "daily": [{"day": "2026-01-01", "t_mean": 1}, {"day": "2026-01-05", "t_mean": 2}],
            "hourly": [{"stamp": "2026-01-03T10:00:00", "temperature": 1}],
        })
        got = (await admin_client.get("/weather/points")).json()[0]
        assert got["daily"] == {"first": "2026-01-01", "last": "2026-01-05", "count": 2}
        assert got["hourly"] == {"first": "2026-01-03", "last": "2026-01-03", "count": 1}


class TestPlan:
    async def test_archive_span_and_missing_runs(self, admin_client, seed_topology):
        b = seed_topology["branch"]
        await _archive(seed_topology["line1"], [date(2026, 1, 1), date(2026, 1, 10)])
        p = await _point(admin_client, b)
        hours = [
            {"stamp": (datetime(2026, 1, 2) + timedelta(hours=h)).isoformat(), "temperature": 0}
            for h in range(24)
        ]
        await admin_client.post("/weather/import", json={
            "point_id": p["id"],
            "daily": [{"day": f"2026-01-0{d}", "t_mean": 0} for d in (3, 4, 5)],
            "hourly": hours,
        })
        plan = (await admin_client.get("/weather/plan", params={"branch_id": b})).json()
        assert plan["archive_first"] == "2026-01-01"
        assert plan["archive_last"] == "2026-01-10"
        [miss] = plan["points"]
        assert miss["daily"] == [
            {"first": "2026-01-01", "last": "2026-01-02"},
            {"first": "2026-01-06", "last": "2026-01-10"},
        ]
        assert miss["hourly"] == [
            {"first": "2026-01-01", "last": "2026-01-01"},
            {"first": "2026-01-03", "last": "2026-01-10"},
        ]

    async def test_a_day_with_23_hours_counts_as_covered(self, admin_client, seed_topology):
        p = await _point(admin_client, seed_topology["branch"])
        hours = [
            {"stamp": (datetime(2026, 3, 29) + timedelta(hours=h)).isoformat(), "temperature": 0}
            for h in range(23)
        ]
        await admin_client.post("/weather/import", json={"point_id": p["id"], "hourly": hours})
        plan = (await admin_client.get("/weather/plan", params={
            "branch_id": seed_topology["branch"],
            "date_from": "2026-03-29", "date_to": "2026-03-29",
        })).json()
        assert plan["points"][0]["hourly"] == []

    async def test_plan_never_asks_for_today(self, admin_client, seed_topology):
        p = await _point(admin_client, seed_topology["branch"])
        today = date.today()
        plan = (await admin_client.get("/weather/plan", params={
            "branch_id": seed_topology["branch"],
            "date_from": (today - timedelta(days=2)).isoformat(),
            "date_to": today.isoformat(),
        })).json()
        [miss] = plan["points"]
        assert miss["point_id"] == p["id"]
        assert miss["daily"][-1]["last"] == (today - timedelta(days=1)).isoformat()


class TestScope:
    async def test_viewer_sees_only_own_branch(
        self, admin_client, scoped_viewer_client, seed_two_branches,
    ):
        mine = await _point(admin_client, seed_two_branches["branch1"], "Своя")
        other = await _point(admin_client, seed_two_branches["branch2"], "Чужа")
        for p in (mine, other):
            await admin_client.post("/weather/import", json={
                "point_id": p["id"], "daily": [{"day": "2026-01-01", "t_mean": 1}],
            })
        listed = (await scoped_viewer_client.get("/weather/points")).json()
        assert [x["id"] for x in listed] == [mine["id"]]
        rows = (await scoped_viewer_client.get("/weather/daily", params={
            "point_id": [mine["id"], other["id"]],
            "date_from": "2026-01-01", "date_to": "2026-01-01",
        })).json()
        assert [r["point_id"] for r in rows] == [mine["id"]]
        plan = await scoped_viewer_client.get(
            "/weather/plan", params={"branch_id": seed_two_branches["branch2"]})
        assert plan.status_code == 404

    async def test_read_span_is_capped(self, admin_client, seed_topology):
        p = await _point(admin_client, seed_topology["branch"])
        resp = await admin_client.get("/weather/daily", params={
            "point_id": p["id"], "date_from": "2020-01-01", "date_to": "2026-01-01",
        })
        assert resp.status_code == 422
