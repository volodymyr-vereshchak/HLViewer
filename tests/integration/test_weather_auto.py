"""Automatic weather update: settings, the server-side download (Open-Meteo
replaced by an httpx MockTransport), failures, and the scheduler's timing."""
from datetime import date, datetime, timedelta

import httpx

from backend.db.engine import async_session_factory
from backend.db.models import DailyArchive
from backend.hl_engine import scheduler_runner
from backend.services import weather_refresh
from backend.services.weather_refresh import plan_requests
from backend.db.models.weather_model import DateRange

TODAY = date(2026, 11, 8)


async def _archive(line_id: int, days: list[date]) -> None:
    async with async_session_factory() as session:
        for d in days:
            session.add(DailyArchive(line_id=line_id, period=d, volume=1, w_volume_dp=1,
                                     pressure=1, temperature=1, density=0.7))
        await session.commit()


def _fake_open_meteo(calls: list):
    """Answers like the archive API: one location per requested coordinate,
    every day / hour of the requested span."""
    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params
        calls.append(dict(q))
        lats = q["latitude"].split(",")
        start, end = date.fromisoformat(q["start_date"]), date.fromisoformat(q["end_date"])
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        locs = []
        for i, lat in enumerate(lats):
            loc = {"latitude": float(lat), "longitude": 35.0}
            if "daily" in q:
                loc["daily"] = {"time": [d.isoformat() for d in days],
                                "temperature_2m_mean": [float(i)] * len(days)}
            if "hourly" in q:
                times = [f"{d.isoformat()}T{h:02d}:00" for d in days for h in range(24)]
                loc["hourly"] = {"time": times, "temperature_2m": [1.5] * len(times)}
            locs.append(loc)
        return httpx.Response(200, json=locs if len(locs) > 1 else locs[0])
    return httpx.MockTransport(handler)


async def _point(client, branch, name, lines=()):
    resp = await client.post("/weather/points", json={
        "branch_id": branch, "name": name, "latitude": 47.8, "longitude": 35.1,
        "lines": [{"kind": "physical", "id": i} for i in lines],
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestSettings:
    async def test_off_by_default_and_editable(self, admin_client):
        st = (await admin_client.get("/weather/auto")).json()
        assert st["auto_enabled"] is False
        assert st["refresh_time"] == "12:00"
        st = (await admin_client.put("/weather/auto", json={"auto_enabled": True, "refresh_time": "06:30"})).json()
        assert (st["auto_enabled"], st["refresh_time"]) == (True, "06:30")

    async def test_bad_time_is_rejected(self, admin_client):
        assert (await admin_client.put("/weather/auto", json={"refresh_time": "25:00"})).status_code == 422

    async def test_viewer_cannot_touch_it(self, viewer_client):
        assert (await viewer_client.get("/weather/auto")).status_code == 403
        assert (await viewer_client.post("/weather/auto/run")).status_code == 403


class TestRun:
    async def test_fetches_only_the_gaps_and_records_the_result(self, admin_client, seed_topology):
        b, l1 = seed_topology["branch"], seed_topology["line1"]
        span = [TODAY - timedelta(days=d) for d in range(20, 9, -1)]  # 19.10 … 29.10
        await _archive(l1, span)
        p1 = await _point(admin_client, b, "A", [l1])
        p2 = await _point(admin_client, b, "B")
        # Point A already has the first three days.
        await admin_client.post("/weather/import", json={
            "point_id": p1["id"], "daily": [{"day": d.isoformat(), "t_mean": 9} for d in span[:3]],
        })

        calls: list = []
        assert await weather_refresh.acquire()
        await weather_refresh.execute_locked(today=TODAY, transport=_fake_open_meteo(calls))

        st = (await admin_client.get("/weather/auto")).json()
        assert st["status"] == "done", st
        daily_calls = [c for c in calls if "daily" in c]
        # B lacks the whole span, so the group asks from its start.
        assert [(c["start_date"], c["end_date"]) for c in daily_calls] == [("2026-10-19", "2026-10-29")]
        assert daily_calls[0]["latitude"].count(",") == 1  # both points, one request
        assert daily_calls[0]["timezone"] == "Europe/Kyiv"

        rows = (await admin_client.get("/weather/daily", params={
            "point_id": [p1["id"], p2["id"]], "date_from": "2026-10-19", "date_to": "2026-10-29",
        })).json()
        assert len(rows) == 22
        # Location order is point order: A got 0.0, B got 1.0.
        assert {r["t_mean"] for r in rows if r["point_id"] == p2["id"]} == {1.0}
        plan = (await admin_client.get("/weather/plan", params={"branch_id": b})).json()
        assert all(not m["daily"] and not m["hourly"] for m in plan["points"])
        assert st["result"] == f"{22} / {2 * 11 * 24}"

    async def test_an_api_refusal_is_reported(self, admin_client, seed_topology):
        await _archive(seed_topology["line1"], [TODAY - timedelta(days=15)])
        await _point(admin_client, seed_topology["branch"], "A")

        def refuse(request):
            return httpx.Response(400, json={"error": True, "reason": "end_date out of range"})
        assert await weather_refresh.acquire()
        await weather_refresh.execute_locked(today=TODAY, transport=httpx.MockTransport(refuse))
        st = (await admin_client.get("/weather/auto")).json()
        assert st["status"] == "error"
        assert "end_date out of range" in st["error"]

    async def test_no_internet_is_reported(self, admin_client, seed_topology):
        await _archive(seed_topology["line1"], [TODAY - timedelta(days=15)])
        await _point(admin_client, seed_topology["branch"], "A")

        def offline(request):
            raise httpx.ConnectError("no route to host")
        assert await weather_refresh.acquire()
        await weather_refresh.execute_locked(today=TODAY, transport=httpx.MockTransport(offline))
        st = (await admin_client.get("/weather/auto")).json()
        assert st["status"] == "error"
        assert "ConnectError" in st["error"]

    async def test_a_running_job_cannot_be_started_twice(self, admin_client):
        assert await weather_refresh.acquire()
        assert (await admin_client.post("/weather/auto/run")).status_code == 409
        assert not await weather_refresh.acquire()


class TestSchedule:
    async def _armed(self, admin_client, monkeypatch, time="12:00"):
        await admin_client.put("/weather/auto", json={"auto_enabled": True, "refresh_time": time})
        started = []

        async def fake_start():
            started.append(True)
            return True
        monkeypatch.setattr(weather_refresh, "start_in_background", fake_start)
        return started

    async def test_off_means_never(self, admin_client, monkeypatch):
        started = await self._armed(admin_client, monkeypatch)
        await admin_client.put("/weather/auto", json={"auto_enabled": False})
        await scheduler_runner.maybe_refresh_weather()
        assert started == []

    async def test_a_passed_slot_runs_once(self, admin_client, monkeypatch):
        slot = (datetime.now() - timedelta(minutes=5)).strftime("%H:%M")
        started = await self._armed(admin_client, monkeypatch, slot)
        await scheduler_runner.maybe_refresh_weather()
        assert started == [True]
        # The run has started since the slot: nothing more is due today.
        assert await weather_refresh.acquire()
        await scheduler_runner.maybe_refresh_weather()
        assert started == [True]


def test_plan_requests_groups_by_ten_and_cuts_by_year():
    r = DateRange
    gaps = [{"daily": [], "hourly": []} for _ in range(12)]
    gaps[11]["daily"] = [r(first=date(2025, 12, 1), last=date(2026, 1, 10))]
    reqs = plan_requests(gaps, date(2026, 1, 5))
    assert reqs == [
        {"kind": "daily", "indexes": [10, 11], "start": date(2025, 12, 1), "end": date(2025, 12, 31)},
        {"kind": "daily", "indexes": [10, 11], "start": date(2026, 1, 1), "end": date(2026, 1, 5)},
    ]
