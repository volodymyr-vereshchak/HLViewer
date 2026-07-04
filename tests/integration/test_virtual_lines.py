"""Virtual lines (rings): CRUD via /virtual_lines/, visible-lines resolution
and the aggregating archive endpoints /hourly_virtual/ + /daily_virtual/."""

from datetime import date, datetime

from backend.db.engine import async_session_factory
from backend.db.models import DailyArchive, HourlyArchive


async def _create_vl(client, seed_topology, name="Кільце-1", **overrides) -> dict:
    payload = {
        "name": name,
        "branch_id": seed_topology["branch"],
        "lumg_id": seed_topology["lumg"],
        "physical_line_ids": [seed_topology["line1"], seed_topology["line2"]],
        "active": True,
    }
    payload.update(overrides)
    resp = await client.post("/virtual_lines/", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestVirtualLineCrud:
    async def test_create_and_list(self, admin_client, seed_topology):
        vl = await _create_vl(admin_client, seed_topology)
        assert vl["physical_line_ids"] == [
            seed_topology["line1"],
            seed_topology["line2"],
        ]

        resp = await admin_client.get("/virtual_lines/")
        body = resp.json()
        assert len(body) == 1
        assert body[0]["name"] == "Кільце-1"

    async def test_update_replaces_members(self, admin_client, seed_topology):
        vl = await _create_vl(admin_client, seed_topology)
        resp = await admin_client.patch(
            f"/virtual_lines/{vl['id']}",
            json={
                "name": "Кільце-1 (нове)",
                "branch_id": seed_topology["branch"],
                "physical_line_ids": [seed_topology["line1"]],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Кільце-1 (нове)"
        assert body["physical_line_ids"] == [seed_topology["line1"]]

    async def test_delete(self, admin_client, seed_topology):
        vl = await _create_vl(admin_client, seed_topology)
        resp = await admin_client.delete(f"/virtual_lines/{vl['id']}")
        assert resp.status_code == 204
        assert (await admin_client.get("/virtual_lines/")).json() == []

    async def test_update_missing_404(self, admin_client, seed_topology):
        resp = await admin_client.patch(
            "/virtual_lines/9999",
            json={
                "name": "x",
                "branch_id": seed_topology["branch"],
                "physical_line_ids": [],
            },
        )
        assert resp.status_code == 404


class TestVisibleLines:
    async def test_virtual_replaces_members(self, admin_client, seed_topology):
        # NOTE: virtual and physical line ids live in separate sequences, so a
        # ring may share a numeric id with a physical line — compare by
        # (id, is_virtual) pairs, like the frontend does.
        vl = await _create_vl(admin_client, seed_topology)
        resp = await admin_client.get("/virtual_lines/visible")
        assert resp.status_code == 200
        body = resp.json()
        # both physical lines are ring members → only the ring is visible
        assert [(r["id"], r["is_virtual"]) for r in body] == [(vl["id"], True)]
        assert body[0]["physical_line_ids"] == [
            seed_topology["line1"],
            seed_topology["line2"],
        ]

    async def test_physical_lines_without_rings(self, admin_client, seed_topology):
        resp = await admin_client.get("/virtual_lines/visible")
        body = resp.json()
        assert {r["id"] for r in body} == {
            seed_topology["line1"],
            seed_topology["line2"],
        }
        assert all(r["is_virtual"] is False for r in body)


class TestHourlyVirtual:
    async def test_aggregates_ring_volumes(self, admin_client, seed_topology):
        vl = await _create_vl(admin_client, seed_topology)
        async with async_session_factory() as session:
            for line_id, volume in (
                (seed_topology["line1"], 100.0),
                (seed_topology["line2"], 50.0),
            ):
                session.add(
                    HourlyArchive(
                        period=datetime(2024, 12, 25, 10),
                        volume=volume,
                        w_volume_dp=1.0,
                        pressure=5.0,
                        temperature=20.0,
                        density=0.7,
                        line_id=line_id,
                    )
                )
            await session.commit()

        resp = await admin_client.get(
            "/hourly_virtual/",
            params={
                "from_date": "2024-12-25T00:00:00",
                "to_date": "2024-12-25T23:00:00",
                "line_id": [vl["id"]],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        row = body[0]
        assert row["line_id"] == vl["id"]
        assert row["volume"] == 150.0  # sum of both members

    async def test_physical_passthrough(self, admin_client, seed_topology):
        async with async_session_factory() as session:
            session.add(
                HourlyArchive(
                    period=datetime(2024, 12, 25, 10),
                    volume=42.0,
                    w_volume_dp=1.0,
                    pressure=5.0,
                    temperature=20.0,
                    density=0.7,
                    line_id=seed_topology["line1"],
                )
            )
            await session.commit()

        resp = await admin_client.get(
            "/hourly_virtual/",
            params={
                "from_date": "2024-12-25T00:00:00",
                "to_date": "2024-12-25T23:00:00",
                "line_id": [seed_topology["line1"]],
            },
        )
        body = resp.json()
        assert len(body) == 1
        assert body[0]["line_id"] == seed_topology["line1"]
        assert body[0]["volume"] == 42.0

    async def test_requires_dates(self, admin_client, seed_topology):
        resp = await admin_client.get("/hourly_virtual/")
        assert resp.status_code == 400


class TestDailyVirtual:
    async def test_aggregates_ring_volumes(self, admin_client, seed_topology):
        vl = await _create_vl(admin_client, seed_topology)
        async with async_session_factory() as session:
            for line_id, volume in (
                (seed_topology["line1"], 1000.0),
                (seed_topology["line2"], 500.0),
            ):
                session.add(
                    DailyArchive(
                        period=date(2024, 12, 25),
                        volume=volume,
                        w_volume_dp=1.0,
                        pressure=5.0,
                        temperature=20.0,
                        density=0.7,
                        line_id=line_id,
                    )
                )
            await session.commit()

        resp = await admin_client.get(
            "/daily_virtual/",
            params={
                "from_date": "2024-12-25T00:00:00",
                "to_date": "2024-12-26T00:00:00",
                "line_id": [vl["id"]],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["volume"] == 1500.0
