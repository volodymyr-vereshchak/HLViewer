"""Archive endpoint tests (/daily/, /hourly/, /sys/, /edit/, /param/):
date-range validation, data retrieval, name joins, pagination and — most
importantly — viewer branch scoping through get_allowed_line_ids."""

from datetime import date, datetime

from backend.db.engine import async_session_factory
from backend.db.models import (
    DailyArchive,
    EditArchive,
    EditType,
    GasVolumeCalc,
    GasVolumeCalcType,
    HourlyArchive,
    Param,
    SysArchive,
    SysType,
)

# Param has ~20 float columns; fill everything numeric with 0.0 by default
_PARAM_FLOATS = {
    name: 0.0
    for name, field in Param.model_fields.items()
    if field.annotation is float
}


async def _add(*rows):
    async with async_session_factory() as session:
        for row in rows:
            session.add(row)
        await session.commit()


def _hour(line_id: int, hour: int, volume: float = 100.0) -> HourlyArchive:
    return HourlyArchive(
        period=datetime(2024, 12, 25, hour),
        volume=volume,
        w_volume_dp=0.1,
        pressure=5.2,
        temperature=20.5,
        density=0.7,
        line_id=line_id,
    )


def _day(line_id: int, day: int, volume: float = 24000.0) -> DailyArchive:
    return DailyArchive(
        period=date(2024, 12, day),
        volume=volume,
        w_volume_dp=2.4,
        pressure=5.2,
        temperature=20.5,
        density=0.7,
        line_id=line_id,
    )


class TestDateValidation:
    async def test_hourly_requires_dates(self, admin_client):
        resp = await admin_client.get("/hourly/")
        assert resp.status_code == 400

    async def test_daily_range_limit_400_days(self, admin_client):
        resp = await admin_client.get(
            "/daily/",
            params={"from_date": "2020-01-01T00:00:00", "to_date": "2024-01-01T00:00:00"},
        )
        assert resp.status_code == 400

    async def test_sys_range_limit_30_days(self, admin_client):
        resp = await admin_client.get(
            "/sys/",
            params={"from_date": "2024-01-01T00:00:00", "to_date": "2024-03-01T00:00:00"},
        )
        assert resp.status_code == 400


class TestHourlyArchive:
    async def test_range_and_line_filter(self, admin_client, seed_topology):
        line1, line2 = seed_topology["line1"], seed_topology["line2"]
        await _add(_hour(line1, 0), _hour(line1, 5), _hour(line2, 3))

        resp = await admin_client.get(
            "/hourly/",
            params={
                "from_date": "2024-12-25T00:00:00",
                "to_date": "2024-12-25T23:00:00",
            },
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 3

        resp = await admin_client.get(
            "/hourly/",
            params={
                "from_date": "2024-12-25T00:00:00",
                "to_date": "2024-12-25T23:00:00",
                "line_id": [line2],
            },
        )
        body = resp.json()
        assert len(body) == 1
        assert body[0]["line_id"] == line2

    async def test_last_period(self, admin_client, seed_topology):
        await _add(_hour(seed_topology["line1"], 0), _hour(seed_topology["line1"], 7))
        resp = await admin_client.get("/hourly_last_period/")
        assert resp.status_code == 200
        assert resp.json()["last_period"] == "2024-12-25T07:00:00"

    async def test_last_period_empty(self, admin_client, seed_topology):
        resp = await admin_client.get("/hourly_last_period/")
        assert resp.json()["last_period"] is None

    async def test_counts(self, admin_client, seed_topology):
        line1 = seed_topology["line1"]
        await _add(_hour(line1, 0), _hour(line1, 0, volume=101.0), _hour(line1, 1))
        resp = await admin_client.get(
            "/hourly_counts/",
            params={
                "from_date": "2024-12-25T00:00:00",
                "to_date": "2024-12-25T23:00:00",
            },
        )
        assert resp.status_code == 200
        counts = {row["hour_group"]: row["record_count"] for row in resp.json()}
        assert counts == {"2024-12-25T00:00:00": 2, "2024-12-25T01:00:00": 1}


class TestDailyArchive:
    async def test_range(self, admin_client, seed_topology):
        line1 = seed_topology["line1"]
        await _add(_day(line1, 20), _day(line1, 21), _day(line1, 25))
        resp = await admin_client.get(
            "/daily/",
            params={"from_date": "2024-12-20T00:00:00", "to_date": "2024-12-22T00:00:00"},
        )
        assert resp.status_code == 200
        assert [r["period"] for r in resp.json()] == ["2024-12-20", "2024-12-21"]


class TestSysArchive:
    async def _seed_sys(self, seed_topology):
        """SysArchive rows + the type-join chain (calc_type → sys_type name)."""
        async with async_session_factory() as session:
            gvct = GasVolumeCalcType(type_id=4, type_name="Тип 4")
            session.add(gvct)
            await session.flush()
            calc = await session.get(GasVolumeCalc, seed_topology["calc"])
            calc.type_id = gvct.id
            session.add(calc)
            session.add(
                SysType(sys_type_id=7, gas_volume_calc_type_id=4, sys_name="Втрата живлення")
            )
            await session.commit()
        await _add(
            SysArchive(
                period=datetime(2024, 12, 25, 10),
                sys_type_id=7,
                volume=1.0,
                line_id=seed_topology["line1"],
            ),
            SysArchive(
                period=datetime(2024, 12, 25, 11),
                sys_type_id=99,  # no SysType row → fallback name
                volume=2.0,
                line_id=seed_topology["line1"],
            ),
        )

    async def test_get_with_sys_name_join(self, admin_client, seed_topology):
        await self._seed_sys(seed_topology)
        resp = await admin_client.get(
            "/sys/",
            params={"from_date": "2024-12-25T00:00:00", "to_date": "2024-12-26T00:00:00"},
        )
        assert resp.status_code == 200
        by_type = {r["sys_type_id"]: r for r in resp.json()}
        assert by_type[7]["sys_name"] == "Втрата живлення"
        assert "99" in by_type[99]["sys_name"]  # unknown-code fallback

    async def test_paged_shape(self, admin_client, seed_topology):
        await self._seed_sys(seed_topology)
        resp = await admin_client.get(
            "/sys/paged/",
            params={
                "from_date": "2024-12-25T00:00:00",
                "to_date": "2024-12-26T00:00:00",
                "limit": 1,
            },
        )
        body = resp.json()
        assert body["total"] == 2
        assert len(body["items"]) == 1

    async def test_grouped(self, admin_client, seed_topology):
        await self._seed_sys(seed_topology)
        resp = await admin_client.get(
            "/sys/grouped/",
            params={"from_date": "2024-12-25T00:00:00", "to_date": "2024-12-26T00:00:00"},
        )
        assert resp.status_code == 200
        groups = {g["sys_type_id"]: g for g in resp.json()}
        assert groups[7]["total_events"] == 1
        assert groups[7]["lines"][0]["line_id"] == seed_topology["line1"]


class TestEditArchive:
    async def test_get_with_edit_name_join(self, admin_client, seed_topology):
        async with async_session_factory() as session:
            gvct = GasVolumeCalcType(type_id=4, type_name="Тип 4")
            session.add(gvct)
            await session.flush()
            calc = await session.get(GasVolumeCalc, seed_topology["calc"])
            calc.type_id = gvct.id
            session.add(calc)
            session.add(
                EditType(edit_type_id=3, gas_volume_calc_type_id=4, edit_name="Зміна уставки")
            )
            await session.commit()
        await _add(
            EditArchive(
                period=datetime(2024, 12, 25, 9),
                old_value=10,
                new_value=20,
                edit_type_id=3,
                line_id=seed_topology["line1"],
            )
        )

        resp = await admin_client.get(
            "/edit/",
            params={"from_date": "2024-12-25T00:00:00", "to_date": "2024-12-26T00:00:00"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["edit_name"] == "Зміна уставки"
        assert body[0]["old_value"] == 10
        assert body[0]["new_value"] == 20


class TestParam:
    async def test_returns_latest_per_line(self, admin_client, seed_topology):
        line1, line2 = seed_topology["line1"], seed_topology["line2"]
        await _add(
            Param(**_PARAM_FLOATS, period=datetime(2024, 12, 1), line_id=line1),
            Param(
                **dict(_PARAM_FLOATS, density=0.68),
                period=datetime(2024, 12, 20),
                line_id=line1,
            ),
            Param(**_PARAM_FLOATS, period=datetime(2024, 12, 10), line_id=line2),
        )
        resp = await admin_client.get("/param/", params={"line_id": [line1, line2]})
        assert resp.status_code == 200
        by_line = {r["line_id"]: r for r in resp.json()}
        assert set(by_line) == {line1, line2}
        assert by_line[line1]["period"] == "2024-12-20T00:00:00"
        assert by_line[line1]["density"] == 0.68

    async def test_single_line_latest(self, admin_client, seed_topology):
        line1 = seed_topology["line1"]
        await _add(
            Param(**_PARAM_FLOATS, period=datetime(2024, 11, 1), line_id=line1),
            Param(**_PARAM_FLOATS, period=datetime(2024, 12, 1), line_id=line1),
        )
        resp = await admin_client.get("/param/", params={"line_id": [line1]})
        body = resp.json()
        assert len(body) == 1
        assert body[0]["period"] == "2024-12-01T00:00:00"

    async def test_range_returns_latest_inside_it(self, admin_client, seed_topology):
        """With both dates the answer is the last snapshot IN the range — not
        the whole history of changes, and not a record from before it."""
        line1 = seed_topology["line1"]
        await _add(
            Param(**_PARAM_FLOATS, period=datetime(2024, 12, 1), line_id=line1),
            Param(**_PARAM_FLOATS, period=datetime(2024, 12, 10), line_id=line1),
            Param(**_PARAM_FLOATS, period=datetime(2024, 12, 20), line_id=line1),
        )
        resp = await admin_client.get(
            "/param/",
            params={
                "line_id": [line1],
                "from_date": "2024-12-05T00:00:00",
                "to_date": "2024-12-15T23:59:59",
            },
        )
        body = resp.json()
        assert len(body) == 1
        assert body[0]["period"] == "2024-12-10T00:00:00"

    async def test_range_without_records_is_empty(self, admin_client, seed_topology):
        line1 = seed_topology["line1"]
        await _add(Param(**_PARAM_FLOATS, period=datetime(2024, 12, 1), line_id=line1))
        resp = await admin_client.get(
            "/param/",
            params={
                "line_id": [line1],
                "from_date": "2024-12-05T00:00:00",
                "to_date": "2024-12-15T00:00:00",
            },
        )
        assert resp.status_code == 200
        assert resp.json() == []


class TestViewerBranchScoping:
    async def test_viewer_sees_only_allowed_branch(
        self, scoped_viewer_client, seed_two_branches
    ):
        line1, line2 = seed_two_branches["line1"], seed_two_branches["line2"]
        await _add(_hour(line1, 0, volume=111.0), _hour(line2, 0, volume=222.0))

        # no explicit line filter → implicitly scoped to branch1's lines
        resp = await scoped_viewer_client.get(
            "/hourly/",
            params={
                "from_date": "2024-12-25T00:00:00",
                "to_date": "2024-12-25T23:00:00",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert [r["line_id"] for r in body] == [line1]
        assert body[0]["volume"] == 111.0

    async def test_viewer_cannot_request_foreign_line(
        self, scoped_viewer_client, seed_two_branches
    ):
        line1, line2 = seed_two_branches["line1"], seed_two_branches["line2"]
        await _add(_hour(line1, 0), _hour(line2, 0))

        # explicitly asking for the foreign line → silently filtered out
        resp = await scoped_viewer_client.get(
            "/hourly/",
            params={
                "from_date": "2024-12-25T00:00:00",
                "to_date": "2024-12-25T23:00:00",
                "line_id": [line2],
            },
        )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_admin_sees_everything(self, seed_two_branches, seed_users):
        # admin_client can't be used directly: seed_users fixture ordering —
        # build the data first, then log in
        from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME
        from httpx import ASGITransport, AsyncClient

        from backend.api.main import app

        line1, line2 = seed_two_branches["line1"], seed_two_branches["line2"]
        await _add(_hour(line1, 0), _hour(line2, 0))

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            login = await client.post(
                "/auth/login",
                json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            )
            assert login.status_code == 200
            resp = await client.get(
                "/hourly/",
                params={
                    "from_date": "2024-12-25T00:00:00",
                    "to_date": "2024-12-25T23:00:00",
                },
            )
        assert {r["line_id"] for r in resp.json()} == {line1, line2}

    async def test_viewer_branch_list_scoped(
        self, scoped_viewer_client, seed_two_branches
    ):
        resp = await scoped_viewer_client.get("/grmu_branch/")
        assert resp.status_code == 200
        assert [b["id"] for b in resp.json()] == [seed_two_branches["branch1"]]

    async def test_viewer_foreign_line_sys_paged_empty(
        self, scoped_viewer_client, seed_two_branches
    ):
        line2 = seed_two_branches["line2"]
        await _add(
            SysArchive(
                period=datetime(2024, 12, 25, 10),
                sys_type_id=7,
                volume=1.0,
                line_id=line2,
            )
        )
        resp = await scoped_viewer_client.get(
            "/sys/paged/",
            params={
                "from_date": "2024-12-25T00:00:00",
                "to_date": "2024-12-26T00:00:00",
                "line_id": [line2],
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"total": 0, "items": []}

    async def test_viewer_foreign_line_param_empty(
        self, scoped_viewer_client, seed_two_branches
    ):
        line2 = seed_two_branches["line2"]
        await _add(
            Param(**_PARAM_FLOATS, period=datetime(2024, 12, 1), line_id=line2)
        )
        resp = await scoped_viewer_client.get("/param/", params={"line_id": [line2]})
        assert resp.status_code == 200
        assert resp.json() == []
