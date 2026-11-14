"""Analytics → GRS consumption models: upload (merge, never twice), status,
training, analysis, station switches and who may do what."""
from datetime import date, timedelta

from tests import grs_model_data as data

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def _upload(client, branch_id, *files):
    return await client.post(
        "/grs-model/upload", params={"branch_id": branch_id},
        files=[("files", (name, content, XLSX)) for name, content in files],
    )


async def _status(client, branch_id) -> dict:
    resp = await client.get("/grs-model/status", params={"branch_id": branch_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _weather(client, branch_id, last=data.LAST):
    point = (await client.post("/weather/points", json={
        "branch_id": branch_id, "name": "Місто", "latitude": 47.84, "longitude": 35.17, "lines": [],
    })).json()
    rows = [{**r, "day": r["day"].isoformat()} for r in data.weather(data.FIRST, last)]
    resp = await client.post("/weather/import", json={"point_id": point["id"], "daily": rows})
    assert resp.status_code == 200, resp.text
    return point["id"]


async def _loaded(client, branch_id):
    inflow, consumption = data.reports()
    resp = await _upload(client, branch_id, ("надходження.xlsx", inflow), ("пром.xlsx", consumption))
    assert resp.status_code == 200, resp.text
    await _weather(client, branch_id)
    return resp.json()["files"]


class TestUpload:
    async def test_files_are_recognised_by_content(self, admin_client, seed_topology):
        files = await _loaded(admin_client, seed_topology["branch"])
        days = len(data.days())
        assert [(f["kind"], f["layout"], f["days"], f["stations"]) for f in files] == [
            ("inflow", "grs", days, 4), ("consumption", "category", days, 4)]
        assert [(f["added"], f["updated"], f["unchanged"]) for f in files] == [
            (4 * days, 0, 0), (4 * days, 0, 0)]

        status = await _status(admin_client, seed_topology["branch"])
        assert [s["name"] for s in status["stations"]] == sorted(data.STATIONS)
        assert status["rows"] == 8 * days
        assert status["inflow"] == {
            "first": data.FIRST.isoformat(), "last": data.LAST.isoformat(), "days": days}
        assert status["stations"][0]["inflow_days"] == days
        assert status["fit"] is None
        assert len(status["uploads"]) == 2

    async def test_same_file_again_adds_nothing(self, admin_client, seed_topology):
        branch = seed_topology["branch"]
        inflow, _ = data.reports(date(2026, 1, 1), date(2026, 1, 31))
        await _upload(admin_client, branch, ("a.xlsx", inflow))
        again = (await _upload(admin_client, branch, ("b.xlsx", inflow))).json()["files"][0]
        assert (again["added"], again["updated"], again["unchanged"]) == (0, 0, 4 * 31)
        assert (await _status(admin_client, branch))["rows"] == 4 * 31

    async def test_longer_file_appends_and_corrections_replace(self, admin_client, seed_topology):
        branch = seed_topology["branch"]
        january = data.days(date(2026, 1, 1), date(2026, 1, 31))
        longer = data.days(date(2026, 1, 1), date(2026, 2, 10))
        await _upload(admin_client, branch, ("січень.xlsx", data.inflow_xlsx(
            {"ГРС Альфа": {d: 100.0 for d in january}}, january)))
        values = {d: 100.0 for d in longer}
        values[date(2026, 1, 15)] = 250.0           # 1С corrected the day since
        result = (await _upload(admin_client, branch, ("лютий.xlsx", data.inflow_xlsx(
            {"ГРС Альфа": values}, longer)))).json()["files"][0]
        assert (result["added"], result["updated"], result["unchanged"]) == (10, 1, 30)
        status = await _status(admin_client, branch)
        assert status["rows"] == 41
        assert status["inflow"]["days"] == 41

    async def test_ring_stations_fall_under_one_name(self, admin_client, seed_topology):
        branch = seed_topology["branch"]
        period = data.days(date(2026, 1, 1), date(2026, 1, 3))
        content = data.consumption_xlsx({
            "ГРС-1 Запоріжжя": {"Промисловість": {period[0]: 10}},
            "ГРС-2 Запоріжжя": {"Промисловість": {period[0]: 5}},
        }, period)
        result = (await _upload(admin_client, branch, ("пром.xlsx", content))).json()["files"][0]
        assert (result["stations"], result["added"]) == (1, 1)
        status = await _status(admin_client, branch)
        assert [s["name"] for s in status["stations"]] == ["Кільце ГРС-1,2,3 Запоріжжя"]

    async def test_unreadable_file_is_reported_and_the_rest_taken(self, admin_client, seed_topology):
        inflow, _ = data.reports(date(2026, 1, 1), date(2026, 1, 5))
        files = (await _upload(admin_client, seed_topology["branch"],
                               ("щось.xlsx", b"not a workbook"), ("ok.xlsx", inflow))).json()["files"]
        assert "Excel" in files[0]["error"]
        assert files[1]["added"] == 20

    async def test_viewer_may_not_upload_or_train(self, viewer_client, seed_topology):
        inflow, _ = data.reports(date(2026, 1, 1), date(2026, 1, 5))
        assert (await _upload(viewer_client, seed_topology["branch"], ("a.xlsx", inflow))).status_code == 403
        assert (await viewer_client.post(
            "/grs-model/train", params={"branch_id": seed_topology["branch"]})).status_code == 403


class TestTrainAndAnalyse:
    async def test_train_then_analyse(self, admin_client, seed_topology):
        branch = seed_topology["branch"]
        await _loaded(admin_client, branch)
        assert (await admin_client.get(
            "/grs-model/analysis", params={"branch_id": branch})).status_code == 409

        trained = await admin_client.post("/grs-model/train", params={"branch_id": branch})
        assert trained.status_code == 200, trained.text
        assert [s["status"] for s in trained.json()["stations"]] == ["ok"] * 4

        status = await _status(admin_client, branch)
        assert status["fit"]["stale"] is False
        assert len(status["fit"]["stations"]) == 4

        params = {"branch_id": branch, "date_from": "2026-01-01", "date_to": "2026-03-31"}
        result = (await admin_client.get("/grs-model/analysis", params=params)).json()
        assert len(result["days"]) == 90
        assert {e["grs"] for e in result["episodes"]} == {data.FAULTY}
        # The same question again is answered from what was computed.
        assert (await admin_client.get("/grs-model/analysis", params=params)).json() == result

    async def test_upload_after_training_marks_the_model_stale(self, admin_client, seed_topology):
        branch = seed_topology["branch"]
        await _loaded(admin_client, branch)
        await admin_client.post("/grs-model/train", params={"branch_id": branch})
        extra = data.days(data.LAST + timedelta(days=1), data.LAST + timedelta(days=3))
        await _upload(admin_client, branch, ("ще.xlsx", data.inflow_xlsx(
            {"ГРС Альфа": {d: 5000.0 for d in extra}}, extra)))
        assert (await _status(admin_client, branch))["fit"]["stale"] is True

    async def test_train_without_weather_says_what_is_missing(self, admin_client, seed_topology):
        inflow, consumption = data.reports()
        await _upload(admin_client, seed_topology["branch"], ("a.xlsx", inflow), ("b.xlsx", consumption))
        resp = await admin_client.post("/grs-model/train", params={"branch_id": seed_topology["branch"]})
        assert resp.status_code == 422
        assert "погоди" in resp.json()["detail"]

    async def test_disabled_station_is_left_out(self, admin_client, seed_topology):
        branch = seed_topology["branch"]
        await _loaded(admin_client, branch)
        station = next(s for s in (await _status(admin_client, branch))["stations"]
                       if s["name"] == "ГРС Бета")
        resp = await admin_client.patch(
            "/grs-model/stations/%d" % station["id"], json={"enabled": False})
        assert resp.json()["enabled"] is False
        trained = (await admin_client.post("/grs-model/train", params={"branch_id": branch})).json()
        assert "ГРС Бета" not in [s["name"] for s in trained["stations"]]

    async def test_station_weather_point_must_be_of_the_branch(self, admin_client, seed_two_branches):
        inflow, _ = data.reports(date(2026, 1, 1), date(2026, 1, 5))
        await _upload(admin_client, seed_two_branches["branch1"], ("a.xlsx", inflow))
        own = await _weather(admin_client, seed_two_branches["branch1"], data.FIRST + timedelta(days=5))
        foreign = (await admin_client.post("/weather/points", json={
            "branch_id": seed_two_branches["branch2"], "name": "Чужа", "latitude": 48, "longitude": 35,
            "lines": [],
        })).json()["id"]
        station = (await _status(admin_client, seed_two_branches["branch1"]))["stations"][0]
        url = "/grs-model/stations/%d" % station["id"]
        assert (await admin_client.patch(url, json={"weather_point_id": foreign})).status_code == 422
        assert (await admin_client.patch(url, json={"weather_point_id": own})).json()["weather_point_id"] == own
        assert (await admin_client.patch(url, json={"weather_point_id": 0})).json()["weather_point_id"] is None


class TestScope:
    async def test_viewer_reads_only_their_branch(self, admin_client, scoped_viewer_client,
                                                  seed_two_branches):
        inflow, _ = data.reports(date(2026, 1, 1), date(2026, 1, 5))
        for branch in (seed_two_branches["branch1"], seed_two_branches["branch2"]):
            await _upload(admin_client, branch, ("a.xlsx", inflow))
        assert len((await _status(scoped_viewer_client, seed_two_branches["branch1"]))["stations"]) == 4
        for path in ("/grs-model/status", "/grs-model/analysis"):
            resp = await scoped_viewer_client.get(path, params={"branch_id": seed_two_branches["branch2"]})
            assert resp.status_code == 404, path
