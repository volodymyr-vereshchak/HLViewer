"""Gas routes: CRUD, branch scoping, and the «Звірка ФХП» report end to end.

The report numbers here are the same ones worked out by hand in
tests/unit/test_fhp_series.py — that is the point: this file proves the two
SQL statements, the seed fallback and the folding agree with the arithmetic.
"""

from datetime import datetime

import pytest

from backend.db.engine import async_session_factory
from sqlalchemy import delete as sa_delete

from backend.db.models import EditArchive, GasVolumeCalc, HourlyArchive, Line
from backend.db.models.gas_route_model import GasRouteMember
from backend.services.edit_value_codec import encode_float

DENSITY = 1
CO2 = 2
N2 = 3

# The archive keeps pressure in the line's own unit — кгс/см² by default. The
# volume numbers below are pinned at 3.2357 MPa, which is this many кгс/см².
KGF_3_2357_MPA = 3.2357e6 / 98066.5


def dt(day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, 5, day, hour, minute)


async def add_changes(line_id: int, rows: list[tuple[datetime, float, float]],
                      edit_type_id: int = DENSITY) -> None:
    """rows = (period, old, new) with real float values."""
    async with async_session_factory() as session:
        for period, old, new in rows:
            session.add(EditArchive(
                line_id=line_id,
                period=period,
                edit_type_id=edit_type_id,
                old_value=encode_float(old),
                new_value=encode_float(new),
            ))
        await session.commit()


async def add_hourly(line_id: int, stamps: list[datetime]) -> None:
    """Hourly rows only mark how far the import has run — the values are
    irrelevant to ФХП, which comes from the change archive."""
    async with async_session_factory() as session:
        for stamp in stamps:
            session.add(HourlyArchive(
                line_id=line_id, period=stamp, volume=1.0, w_volume_dp=1.0,
                pressure=1.0, temperature=1.0, density=0.74,
            ))
        await session.commit()


async def add_line(calc_id: int, number: int, name: str) -> int:
    async with async_session_factory() as session:
        line = Line(line=number, name=name, meter=False, gas_volume_calc_id=calc_id)
        session.add(line)
        await session.commit()
        return line.id


def one_reference(seed_topology) -> list[dict]:
    """The smallest route the API will accept: one line, marked reference."""
    return [{"line_id": seed_topology["line1"], "is_reference": True}]


async def create_route(client, seed_topology, members, number="301", **overrides):
    payload = {
        "number": number,
        "name": "Тестовий маршрут",
        "branch_id": seed_topology["branch"],
        "active": True,
        "members": members,
    }
    payload.update(overrides)
    return await client.post("/gas_routes/", json=payload)


def block(body: dict, param: str = "density") -> dict:
    return next(b for b in body["params"] if b["param"] == param)


def line_of(blk: dict, line_id: int) -> dict:
    return next(line for line in blk["lines"] if line["line_id"] == line_id)


def at(blk: dict, period: str):
    return blk["periods"].index(period)


class TestRouteCrud:
    async def test_create_and_list(self, admin_client, seed_topology):
        resp = await create_route(admin_client, seed_topology, [
            {"line_id": seed_topology["line1"], "is_reference": True},
            {"line_id": seed_topology["line2"], "is_reference": False},
        ])
        assert resp.status_code == 201, resp.text
        route = resp.json()
        assert [m["line_id"] for m in route["members"]] == [
            seed_topology["line1"], seed_topology["line2"]
        ]
        assert [m["is_reference"] for m in route["members"]] == [True, False]
        assert route["members"][0]["line_name"] == "l1"

        listed = (await admin_client.get("/gas_routes/")).json()
        assert len(listed) == 1 and listed[0]["number"] == "301"

    async def test_update_replaces_members_and_keeps_flags(
        self, admin_client, seed_topology
    ):
        route = (await create_route(admin_client, seed_topology, [
            {"line_id": seed_topology["line1"], "is_reference": True},
        ])).json()

        resp = await admin_client.patch(f"/gas_routes/{route['id']}", json={
            "number": "302",
            "branch_id": seed_topology["branch"],
            "active": True,
            "members": [
                {"line_id": seed_topology["line2"], "is_reference": True},
                {"line_id": seed_topology["line1"], "is_reference": False},
            ],
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["number"] == "302"
        assert [(m["line_id"], m["is_reference"]) for m in body["members"]] == [
            (seed_topology["line2"], True), (seed_topology["line1"], False)
        ]

    async def test_delete(self, admin_client, seed_topology):
        route = (await create_route(
            admin_client, seed_topology, one_reference(seed_topology)
        )).json()
        assert (await admin_client.delete(f"/gas_routes/{route['id']}")).status_code == 204
        assert (await admin_client.get("/gas_routes/")).json() == []

    async def test_missing_route_is_404(self, admin_client, seed_topology):
        assert (await admin_client.get("/gas_routes/9999")).status_code == 404

    async def test_duplicate_number_in_the_same_branch_is_409(
        self, admin_client, seed_topology
    ):
        await create_route(admin_client, seed_topology, one_reference(seed_topology))
        resp = await create_route(
            admin_client, seed_topology,
            [{"line_id": seed_topology["line2"], "is_reference": True}],
        )
        assert resp.status_code == 409
        assert "301" in resp.json()["detail"]

    async def test_same_number_in_another_branch_is_fine(
        self, admin_client, seed_two_branches
    ):
        for n in (1, 2):
            resp = await admin_client.post("/gas_routes/", json={
                "number": "301", "branch_id": seed_two_branches[f"branch{n}"],
                "active": True,
                "members": [
                    {"line_id": seed_two_branches[f"line{n}"], "is_reference": True}
                ],
            })
            assert resp.status_code == 201, resp.text

    async def test_a_line_cannot_be_in_two_routes(self, admin_client, seed_topology):
        await create_route(admin_client, seed_topology, [
            {"line_id": seed_topology["line1"], "is_reference": True},
        ])
        resp = await create_route(admin_client, seed_topology, [
            {"line_id": seed_topology["line1"], "is_reference": True},
        ], number="302")
        assert resp.status_code == 400
        assert "301" in resp.json()["detail"]

    async def test_the_same_line_twice_in_one_payload(self, admin_client, seed_topology):
        resp = await create_route(admin_client, seed_topology, [
            {"line_id": seed_topology["line1"], "is_reference": False},
            {"line_id": seed_topology["line1"], "is_reference": True},
        ])
        assert resp.status_code == 400

    async def test_line_from_another_branch_is_rejected(
        self, admin_client, seed_two_branches
    ):
        resp = await admin_client.post("/gas_routes/", json={
            "number": "301", "branch_id": seed_two_branches["branch1"],
            "active": True,
            "members": [{"line_id": seed_two_branches["line2"], "is_reference": True}],
        })
        assert resp.status_code == 400
        assert "філії" in resp.json()["detail"]

    async def test_empty_number_is_rejected(self, admin_client, seed_topology):
        resp = await create_route(
            admin_client, seed_topology, one_reference(seed_topology), number="   "
        )
        assert resp.status_code == 400

    async def test_free_lines_excludes_lines_of_other_routes(
        self, admin_client, seed_topology
    ):
        await create_route(admin_client, seed_topology, [
            {"line_id": seed_topology["line1"], "is_reference": True},
        ])
        free = (await admin_client.get(
            "/gas_routes/free_lines/", params={"branch_id": seed_topology["branch"]}
        )).json()
        assert [line["id"] for line in free] == [seed_topology["line2"]]

    async def test_free_lines_keeps_the_edited_routes_own_members(
        self, admin_client, seed_topology
    ):
        route = (await create_route(admin_client, seed_topology, [
            {"line_id": seed_topology["line1"], "is_reference": True},
        ])).json()
        free = (await admin_client.get("/gas_routes/free_lines/", params={
            "branch_id": seed_topology["branch"], "route_id": route["id"],
        })).json()
        assert {line["id"] for line in free} == {
            seed_topology["line1"], seed_topology["line2"]
        }


class TestBranchScoping:
    async def test_viewer_sees_only_its_own_branch(
        self, admin_client, scoped_viewer_client, seed_two_branches
    ):
        for n in (1, 2):
            await admin_client.post("/gas_routes/", json={
                "number": f"30{n}", "branch_id": seed_two_branches[f"branch{n}"],
                "active": True,
                "members": [
                    {"line_id": seed_two_branches[f"line{n}"], "is_reference": True}
                ],
            })
        listed = (await scoped_viewer_client.get("/gas_routes/")).json()
        assert [r["number"] for r in listed] == ["301"]

    async def test_foreign_route_is_404_not_403(
        self, admin_client, scoped_viewer_client, seed_two_branches
    ):
        other = (await admin_client.post("/gas_routes/", json={
            "number": "302", "branch_id": seed_two_branches["branch2"],
            "active": True,
            "members": [{"line_id": seed_two_branches["line2"], "is_reference": True}],
        })).json()
        assert (await scoped_viewer_client.get(
            f"/gas_routes/{other['id']}"
        )).status_code == 404
        assert (await scoped_viewer_client.get(
            f"/gas_routes/{other['id']}/fhp_report",
            params={"date_from": "2026-05-01", "date_to": "2026-05-02"},
        )).status_code == 404

    async def test_viewer_cannot_write(self, scoped_viewer_client, seed_two_branches):
        resp = await scoped_viewer_client.post("/gas_routes/", json={
            "number": "999", "branch_id": seed_two_branches["branch1"],
            "active": True,
            "members": [{"line_id": seed_two_branches["line1"], "is_reference": True}],
        })
        assert resp.status_code == 403


class TestFhpReport:
    async def report(self, client, route_id, **params):
        query = {"date_from": "2026-05-01", "date_to": "2026-05-02"}
        query.update(params)
        resp = await client.get(f"/gas_routes/{route_id}/fhp_report", params=query)
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def test_hourly_weighting_and_deviation(self, admin_client, seed_topology):
        ref, manual = seed_topology["line1"], seed_topology["line2"]
        # Reference: 0.70 from the start of the commercial day, 0.80 from 07:30.
        await add_changes(ref, [
            (dt(1, 6), 0.60, 0.70),
            (dt(1, 7, 30), 0.70, 0.80),
        ])
        # Manual line: one value for the whole period.
        await add_changes(manual, [(dt(1, 6), 0.60, 0.74)])

        route = (await create_route(admin_client, seed_topology, [
            {"line_id": ref, "is_reference": True},
            {"line_id": manual, "is_reference": False},
        ])).json()

        body = await self.report(admin_client, route["id"])
        blk = block(body)
        assert blk["has_reference"] is True
        i = at(blk, dt(1, 7).isoformat())

        # 30 minutes at 0.70 and 30 at 0.80.
        assert line_of(blk, ref)["values"][i] == pytest.approx(0.75)
        assert blk["reference"][i] == pytest.approx(0.75)
        assert blk["reference_count"][i] == 1

        manual_line = line_of(blk, manual)
        assert manual_line["values"][i] == pytest.approx(0.74)
        assert manual_line["deltas"][i] == pytest.approx(-0.01)
        assert manual_line["delta_pcts"][i] == pytest.approx(-0.01 / 0.75 * 100, rel=1e-3)
        assert manual_line["stats"]["n"] == len(blk["periods"])

    async def test_seed_comes_from_before_the_range(self, admin_client, seed_topology):
        line = seed_topology["line1"]
        # The only change is three days before the range opens.
        await add_changes(line, [(dt(1, 0), 0.60, 0.7467)])
        route = (await create_route(admin_client, seed_topology, [
            {"line_id": line, "is_reference": True},
        ])).json()

        blk = block(await self.report(
            admin_client, route["id"], date_from="2026-05-04", date_to="2026-05-04"
        ))
        assert blk["lines"][0]["status"] == "ok"
        assert blk["lines"][0]["values"][0] == pytest.approx(0.7467)

    async def test_old_value_covers_the_hours_before_the_first_change(
        self, admin_client, seed_topology
    ):
        line = seed_topology["line1"]
        # Nothing before the range: the first in-range change tells us what it
        # replaced, and that value must hold the earlier hours.
        await add_changes(line, [(dt(1, 12), 0.7000, 0.8000)])
        route = (await create_route(admin_client, seed_topology, [
            {"line_id": line, "is_reference": True},
        ])).json()

        blk = block(await self.report(admin_client, route["id"]))
        assert blk["lines"][0]["values"][at(blk, dt(1, 7).isoformat())] == pytest.approx(0.70)
        assert blk["lines"][0]["values"][at(blk, dt(1, 13).isoformat())] == pytest.approx(0.80)

    async def test_all_reference_route_shows_the_spread(
        self, admin_client, seed_topology
    ):
        a, b = seed_topology["line1"], seed_topology["line2"]
        await add_changes(a, [(dt(1, 6), 0.60, 0.74)])
        await add_changes(b, [(dt(1, 6), 0.60, 0.76)])
        route = (await create_route(admin_client, seed_topology, [
            {"line_id": a, "is_reference": True},
            {"line_id": b, "is_reference": True},
        ])).json()

        blk = block(await self.report(admin_client, route["id"]))
        # Nothing to compare against: every line IS the reference.
        assert blk["has_reference"] is False
        assert blk["reference"] is None
        i = at(blk, dt(1, 7).isoformat())
        assert blk["spread_min"][i] == pytest.approx(0.74)
        assert blk["spread_max"][i] == pytest.approx(0.76)
        assert blk["spread"][i] == pytest.approx(0.02)
        assert all(line["deltas"] is None for line in blk["lines"])

    async def test_a_line_without_changes_is_no_data(self, admin_client, seed_topology):
        ref, silent = seed_topology["line1"], seed_topology["line2"]
        await add_changes(ref, [(dt(1, 6), 0.60, 0.74)])
        route = (await create_route(admin_client, seed_topology, [
            {"line_id": ref, "is_reference": True},
            {"line_id": silent, "is_reference": False},
        ])).json()

        body = await self.report(admin_client, route["id"])
        blk = block(body)
        silent_line = line_of(blk, silent)
        assert silent_line["status"] == "no_data"
        assert set(silent_line["values"]) == {None}
        assert silent_line["stats"] is None
        # It must not drag the spread either.
        i = at(blk, dt(1, 7).isoformat())
        assert blk["spread"][i] == pytest.approx(0.0)
        assert any("немає даних" in w for w in body["warnings"])

    async def test_implausible_value_is_not_applied(self, admin_client, seed_topology):
        line = seed_topology["line1"]
        await add_changes(line, [
            (dt(1, 6), 0.60, 0.7467),
            (dt(1, 8), 0.7467, 1e30),
        ])
        route = (await create_route(admin_client, seed_topology, [
            {"line_id": line, "is_reference": True},
        ])).json()

        body = await self.report(admin_client, route["id"])
        blk = block(body)
        assert blk["rejected_changes"] == 1
        # The previous value keeps holding instead.
        assert blk["lines"][0]["values"][at(blk, dt(1, 9).isoformat())] == pytest.approx(0.7467)
        assert any("неправдоподібних" in w for w in body["warnings"])

    async def test_daily_granularity(self, admin_client, seed_topology):
        line = seed_topology["line1"]
        await add_changes(line, [(dt(1, 0), 0.60, 0.74)])
        route = (await create_route(admin_client, seed_topology, [
            {"line_id": line, "is_reference": True},
        ])).json()

        blk = block(await self.report(admin_client, route["id"], granularity="daily"))
        assert blk["periods"] == ["2026-05-01", "2026-05-02"]
        assert blk["hours_present"] == [24, 24]
        assert blk["lines"][0]["values"] == [pytest.approx(0.74), pytest.approx(0.74)]

    async def test_all_three_parameters_come_back(self, admin_client, seed_topology):
        line = seed_topology["line1"]
        await add_changes(line, [(dt(1, 6), 0.60, 0.7467)], edit_type_id=DENSITY)
        await add_changes(line, [(dt(1, 6), 0.50, 0.6130)], edit_type_id=CO2)
        await add_changes(line, [(dt(1, 6), 1.80, 1.9546)], edit_type_id=N2)
        route = (await create_route(admin_client, seed_topology, [
            {"line_id": line, "is_reference": True},
        ])).json()

        body = await self.report(admin_client, route["id"])
        assert [b["param"] for b in body["params"]] == ["density", "co2", "n2"]
        i = at(block(body), dt(1, 7).isoformat())
        assert block(body, "co2")["lines"][0]["values"][i] == pytest.approx(0.6130)
        assert block(body, "n2")["lines"][0]["values"][i] == pytest.approx(1.9546)

    async def test_stale_marking(self, admin_client, seed_topology):
        line = seed_topology["line1"]
        await add_changes(line, [(dt(1, 6), 0.60, 0.74)])
        route = (await create_route(admin_client, seed_topology, [
            {"line_id": line, "is_reference": True},
        ])).json()

        blk = block(await self.report(
            admin_client, route["id"], date_from="2026-05-01", date_to="2026-05-05",
            stale_after_hours=48,
        ))
        assert blk["lines"][0]["stale"][at(blk, dt(1, 7).isoformat())] is False
        assert blk["lines"][0]["stale"][at(blk, dt(4, 7).isoformat())] is True

    async def test_range_is_cut_at_the_last_imported_hour(
        self, admin_client, seed_topology
    ):
        """A step function would hold its value forever; the archive does not."""
        line = seed_topology["line1"]
        await add_changes(line, [(dt(1, 6), 0.60, 0.74)])
        await add_hourly(line, [dt(1, 12), dt(1, 13)])
        route = (await create_route(admin_client, seed_topology, [
            {"line_id": line, "is_reference": True},
        ])).json()

        body = await self.report(
            admin_client, route["id"], date_from="2026-05-01", date_to="2026-05-05"
        )
        blk = block(body)
        # Data ends at the 13:00 record, which covers 13:00–14:00.
        assert blk["periods"][-1] == dt(1, 13).isoformat()
        assert body["data_until"] == dt(1, 13).isoformat()
        assert body["range_clipped_at"] == dt(1, 14).isoformat()
        # Carried by those two fields only — repeating it as a warning gave the
        # screen two alerts for one fact. (Other warnings, e.g. "this line has
        # no CO₂ data", are unrelated and still expected.)
        assert not any("обмежено" in w for w in body["warnings"])

    async def test_daily_axis_stops_at_the_data_too(self, admin_client, seed_topology):
        """The hourly axis walks the clipped range, so it stopped by itself; the
        daily one was built from the REQUESTED dates and padded the table and
        the chart with empty rows for days the archive has not reached."""
        line = seed_topology["line1"]
        await add_changes(line, [(dt(1, 6), 0.60, 0.74)])
        await add_hourly(line, [dt(1, 12), dt(2, 12)])
        route = (await create_route(admin_client, seed_topology, [
            {"line_id": line, "is_reference": True},
        ])).json()

        body = await self.report(
            admin_client, route["id"],
            date_from="2026-05-01", date_to="2026-05-10", granularity="daily",
        )
        blk = block(body)
        # Data reaches 02.05 12:00, so the axis ends on the 2nd — not the 10th.
        assert blk["periods"] == ["2026-05-01", "2026-05-02"]

    async def test_a_period_entirely_past_the_data_is_400(
        self, admin_client, seed_topology
    ):
        line = seed_topology["line1"]
        await add_changes(line, [(dt(1, 6), 0.60, 0.74)])
        await add_hourly(line, [dt(1, 12)])
        route = (await create_route(admin_client, seed_topology, [
            {"line_id": line, "is_reference": True},
        ])).json()

        resp = await admin_client.get(f"/gas_routes/{route['id']}/fhp_report", params={
            "date_from": "2026-06-01", "date_to": "2026-06-02",
        })
        assert resp.status_code == 400
        assert "01.05.2026 12:00" in resp.json()["detail"]

    async def test_data_until_endpoint(self, admin_client, seed_topology):
        line = seed_topology["line1"]
        await add_hourly(line, [dt(1, 12), dt(2, 5)])
        route = (await create_route(admin_client, seed_topology, [
            {"line_id": line, "is_reference": True},
        ])).json()

        resp = await admin_client.get(f"/gas_routes/{route['id']}/data_until")
        assert resp.status_code == 200
        assert resp.json()["data_until"] == dt(2, 5).isoformat()

    async def seed_volume_route(self, admin_client, seed_topology, *, meter=False):
        """A chromatograph line and a manual one, the manual one flowing.

        Compositions are picked so the deviation is the real one from the dev
        data: 0.7424 entered against 0.7467 reference.
        """
        ref, manual = seed_topology["line1"], seed_topology["line2"]
        for line, rho in ((ref, 0.7467), (manual, 0.7424)):
            await add_changes(line, [(dt(1, 6), 0.60, rho)], edit_type_id=DENSITY)
            await add_changes(line, [(dt(1, 6), 0.50, 0.613)], edit_type_id=CO2)
            await add_changes(line, [(dt(1, 6), 1.80, 1.9546)], edit_type_id=N2)
        # 1000 m³ in each of two hours, at the pressure and temperature the
        # unit test pins its numbers to.
        async with async_session_factory() as session:
            for stamp in (dt(1, 7), dt(1, 8)):
                session.add(HourlyArchive(
                    line_id=manual, period=stamp, volume=1000.0, w_volume_dp=100.0,
                    pressure=KGF_3_2357_MPA, temperature=0.2565, density=0.7424,
                ))
            await session.commit()

        if meter:
            async with async_session_factory() as session:
                line = await session.get(Line, manual)
                line.meter = True
                await session.commit()

        return (await create_route(admin_client, seed_topology, [
            {"line_id": ref, "is_reference": True},
            {"line_id": manual, "is_reference": False},
        ])).json(), manual

    async def test_volume_delta_for_an_orifice(self, admin_client, seed_topology):
        route, manual = await self.seed_volume_route(admin_client, seed_topology)
        body = await self.report(admin_client, route["id"])

        vol = body["volume"]
        line = next(line for line in vol["lines"] if line["line_id"] == manual)
        assert line["is_meter"] is False
        assert line["status"] == "ok"
        assert line["total_volume"] == pytest.approx(2000.0)
        # −0.2151 % per hour, the number pinned in tests/unit/test_volume_delta.
        assert line["total_delta"] == pytest.approx(2 * -2.1513, abs=0.01)
        assert line["total_delta_pct"] == pytest.approx(-0.2151, abs=0.001)
        assert vol["total_delta"] == pytest.approx(line["total_delta"], abs=0.01)

        i = vol["periods"].index(dt(1, 7).isoformat())
        assert line["volume"][i] == pytest.approx(1000.0)
        assert line["delta"][i] == pytest.approx(-2.1513, abs=0.01)

    async def test_volume_delta_for_a_meter_is_smaller(
        self, admin_client, seed_topology
    ):
        route, manual = await self.seed_volume_route(
            admin_client, seed_topology, meter=True
        )
        body = await self.report(admin_client, route["id"])
        line = next(
            line for line in body["volume"]["lines"] if line["line_id"] == manual
        )
        assert line["is_meter"] is True
        # Density does not enter a meter's conversion; only K moves, and it
        # moves the other way.
        assert line["total_delta_pct"] == pytest.approx(0.1469, abs=0.001)

    async def test_hours_without_flow_contribute_nothing(
        self, admin_client, seed_topology
    ):
        route, manual = await self.seed_volume_route(admin_client, seed_topology)
        async with async_session_factory() as session:
            session.add(HourlyArchive(
                line_id=manual, period=dt(1, 9), volume=0.0, w_volume_dp=0.0,
                pressure=KGF_3_2357_MPA, temperature=0.2565, density=0.7424,
            ))
            await session.commit()

        body = await self.report(admin_client, route["id"])
        vol = body["volume"]
        line = next(line for line in vol["lines"] if line["line_id"] == manual)
        i = vol["periods"].index(dt(1, 9).isoformat())
        assert line["volume"][i] == pytest.approx(0.0)
        assert line["delta"][i] == pytest.approx(0.0)
        # The zero hour must not dilute the total either.
        assert line["total_delta"] == pytest.approx(2 * -2.1513, abs=0.01)

    async def test_daily_volume_delta_is_the_sum_of_hours(
        self, admin_client, seed_topology
    ):
        route, manual = await self.seed_volume_route(admin_client, seed_topology)
        body = await self.report(admin_client, route["id"], granularity="daily")
        vol = body["volume"]
        line = next(line for line in vol["lines"] if line["line_id"] == manual)
        i = vol["periods"].index("2026-05-01")
        assert line["volume"][i] == pytest.approx(2000.0)
        assert line["delta"][i] == pytest.approx(2 * -2.1513, abs=0.01)

    async def test_route_without_a_reference_has_no_volume_block(
        self, admin_client, seed_topology
    ):
        a, b = seed_topology["line1"], seed_topology["line2"]
        await add_changes(a, [(dt(1, 6), 0.60, 0.74)])
        await add_changes(b, [(dt(1, 6), 0.60, 0.76)])
        route = (await create_route(admin_client, seed_topology, [
            {"line_id": a, "is_reference": True},
            {"line_id": b, "is_reference": True},
        ])).json()
        body = await self.report(admin_client, route["id"])
        assert body["volume"] is None

    async def test_route_without_lines_is_400(self, admin_client, seed_topology):
        # Unreachable through the API now — a route is saved with a reference
        # line — but a member still disappears when its line is deleted, and
        # the report must say so rather than crash.
        route = (await create_route(
            admin_client, seed_topology, one_reference(seed_topology)
        )).json()
        async with async_session_factory() as session:
            await session.execute(
                sa_delete(GasRouteMember).where(
                    GasRouteMember.route_id == route["id"]
                )
            )
            await session.commit()
        resp = await admin_client.get(f"/gas_routes/{route['id']}/fhp_report", params={
            "date_from": "2026-05-01", "date_to": "2026-05-02",
        })
        assert resp.status_code == 400
        assert "не містить ліній" in resp.json()["detail"]

    async def test_reversed_dates_are_400(self, admin_client, seed_topology):
        route = (await create_route(
            admin_client, seed_topology, one_reference(seed_topology)
        )).json()
        resp = await admin_client.get(f"/gas_routes/{route['id']}/fhp_report", params={
            "date_from": "2026-05-05", "date_to": "2026-05-01",
        })
        assert resp.status_code == 400


class TestReferenceIsRequired:
    """A route exists to compare its lines against a reference composition, so
    one has to be marked. It is not necessarily the chromatograph line — a route
    without a chromatograph still takes some line's ФХП as correct."""

    async def test_no_reference_is_rejected(self, admin_client, seed_topology):
        resp = await create_route(admin_client, seed_topology, [
            {"line_id": seed_topology["line1"], "is_reference": False},
            {"line_id": seed_topology["line2"], "is_reference": False},
        ])
        assert resp.status_code == 400
        assert "еталон" in resp.json()["detail"].lower()

    async def test_a_route_without_lines_is_rejected(self, admin_client, seed_topology):
        resp = await create_route(admin_client, seed_topology, [])
        assert resp.status_code == 400

    async def test_the_reference_cannot_be_removed_by_an_update(
        self, admin_client, seed_topology
    ):
        route = (await create_route(
            admin_client, seed_topology, one_reference(seed_topology)
        )).json()
        resp = await admin_client.patch(f"/gas_routes/{route['id']}", json={
            "number": route["number"],
            "branch_id": seed_topology["branch"],
            "active": True,
            "members": [{"line_id": seed_topology["line1"], "is_reference": False}],
        })
        assert resp.status_code == 400
        # The stored route is untouched.
        stored = (await admin_client.get(f"/gas_routes/{route['id']}")).json()
        assert stored["members"][0]["is_reference"] is True
