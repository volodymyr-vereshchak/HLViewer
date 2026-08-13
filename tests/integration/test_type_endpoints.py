"""CRUD tests for the reference-type endpoints:
/sys-types/, /edit-types/ and /gas-volume-calc-types/."""

from datetime import datetime

import pytest_asyncio

from backend.db.engine import async_session_factory
from backend.db.models import GasVolumeCalc, Line, Lumg
from backend.db.models.gas_volume_calc_type_model import GasVolumeCalcType
from backend.db.models.grmu_branch_model import GrmuBranch
from backend.db.models.sys_archive_model import SysArchive
from backend.db.models.sys_type_model import SysType


@pytest_asyncio.fixture
async def archive_on_two_calc_types(clean_db) -> dict:
    """Two calculator types, one line each, and one accident row per line —
    both carrying the SAME event code 5.

    That collision is the point: the code alone is not the identity of an
    event, so a usage count that ignores the calculator type reports two.
    """
    async with async_session_factory() as session:
        branch = GrmuBranch(name="Філія")
        session.add(branch)
        await session.flush()
        lumg = Lumg(name="LUMG", branch_id=branch.id)
        session.add(lumg)
        await session.flush()

        ids = {}
        for n, code in ((1, 9), (2, 4)):
            calc_type = GasVolumeCalcType(type_id=code, type_name=f"Тип {code}")
            session.add(calc_type)
            await session.flush()
            calc = GasVolumeCalc(
                address=10 * n, name=f"a{n}", c_time=7, lumg_id=lumg.id, type_id=calc_type.id
            )
            session.add(calc)
            await session.flush()
            line = Line(line=1, name=f"l{n}", meter=False, gas_volume_calc_id=calc.id)
            session.add(line)
            await session.flush()
            session.add(SysArchive(
                period=datetime(2026, 5, 20, 8), sys_type_id=5, volume=1.0, line_id=line.id
            ))
            ids[f"calc_type_code{n}"] = code

        sys_type = SysType(sys_type_id=5, gas_volume_calc_type_id=9, sys_name="Аварія")
        session.add(sys_type)
        await session.commit()
        ids["sys_type"] = sys_type.id
        return ids


class TestSysTypes:
    _payload = {"sys_type_id": 7, "gas_volume_calc_type_id": 4, "sys_name": "Втрата живлення"}

    async def test_create_and_paged_list(self, admin_client):
        resp = await admin_client.post("/sys-types/", json=self._payload)
        assert resp.status_code == 201

        resp = await admin_client.get("/sys-types/")
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["sys_name"] == "Втрата живлення"

    async def test_duplicate_409(self, admin_client):
        assert (await admin_client.post("/sys-types/", json=self._payload)).status_code == 201
        resp = await admin_client.post("/sys-types/", json=self._payload)
        assert resp.status_code == 409
        assert resp.json()["detail"] == (
            "Подія з таким кодом для цього типу обчислювача вже існує"
        )

    async def test_search_and_calc_type_filter(self, admin_client):
        await admin_client.post("/sys-types/", json=self._payload)
        await admin_client.post(
            "/sys-types/",
            json={"sys_type_id": 8, "gas_volume_calc_type_id": 5, "sys_name": "Перевищення тиску"},
        )

        resp = await admin_client.get("/sys-types/", params={"search": "живлення"})
        assert resp.json()["total"] == 1

        resp = await admin_client.get("/sys-types/", params={"calc_type_id": 5})
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["sys_type_id"] == 8

    async def test_update_and_delete(self, admin_client):
        created = (await admin_client.post("/sys-types/", json=self._payload)).json()

        # SysTypeUpdate still requires the id fields (only sys_name is optional)
        resp = await admin_client.patch(
            f"/sys-types/{created['id']}",
            json=dict(self._payload, sys_name="Нова назва"),
        )
        assert resp.status_code == 200
        assert resp.json()["sys_name"] == "Нова назва"

        assert (await admin_client.delete(f"/sys-types/{created['id']}")).status_code == 204
        assert (await admin_client.get("/sys-types/")).json()["total"] == 0

    async def test_delete_missing_404(self, admin_client):
        assert (await admin_client.delete("/sys-types/9999")).status_code == 404

    async def test_rename_onto_an_existing_code_pair_409(self, admin_client):
        """A typo in the code field, not a server fault: this used to surface as
        a 500 because the DAO does not translate IntegrityError on update."""
        first = (await admin_client.post("/sys-types/", json=self._payload)).json()
        await admin_client.post(
            "/sys-types/",
            json={"sys_type_id": 8, "gas_volume_calc_type_id": 4, "sys_name": "Інша"},
        )
        resp = await admin_client.patch(
            f"/sys-types/{first['id']}", json=dict(self._payload, sys_type_id=8)
        )
        assert resp.status_code == 409
        # Read in the admin panel by someone who mistyped a code, so it says so
        # in words instead of quoting the Postgres constraint.
        assert resp.json()["detail"] == (
            "Подія з таким кодом для цього типу обчислювача вже існує"
        )


class TestTypeUsage:
    async def test_counts_only_the_archive_of_its_own_calculator_type(
        self, admin_client, archive_on_two_calc_types
    ):
        resp = await admin_client.get(f"/sys-types/{archive_on_two_calc_types['sys_type']}/usage")
        assert resp.status_code == 200
        # Two archive rows carry code 5; only one is on a line whose calculator
        # type is 9, which is the type this dictionary entry belongs to.
        assert resp.json() == {"archive_rows": 1, "capped": False}

    async def test_zero_for_a_type_nothing_reported(self, admin_client):
        created = (await admin_client.post(
            "/sys-types/",
            json={"sys_type_id": 77, "gas_volume_calc_type_id": 4, "sys_name": "Тиша"},
        )).json()
        resp = await admin_client.get(f"/sys-types/{created['id']}/usage")
        assert resp.json() == {"archive_rows": 0, "capped": False}

    async def test_missing_type_404(self, admin_client):
        assert (await admin_client.get("/sys-types/9999/usage")).status_code == 404


class TestEditTypes:
    _payload = {"edit_type_id": 3, "gas_volume_calc_type_id": 4, "edit_name": "Зміна уставки"}

    async def test_crud_cycle(self, admin_client):
        created = await admin_client.post("/edit-types/", json=self._payload)
        assert created.status_code == 201
        entry = created.json()

        assert (await admin_client.post("/edit-types/", json=self._payload)).status_code == 409

        listed = (await admin_client.get("/edit-types/")).json()
        assert listed["total"] == 1

        patched = await admin_client.patch(
            f"/edit-types/{entry['id']}",
            json=dict(self._payload, edit_name="Оновлено"),
        )
        assert patched.json()["edit_name"] == "Оновлено"

        assert (await admin_client.delete(f"/edit-types/{entry['id']}")).status_code == 204
        assert (await admin_client.get("/edit-types/")).json()["total"] == 0


class TestGasVolumeCalcTypes:
    _payload = {"type_id": 4, "type_name": "Флоутек-ТМ"}

    async def test_crud_cycle(self, admin_client):
        created = await admin_client.post("/gas-volume-calc-types/", json=self._payload)
        assert created.status_code == 201

        assert (
            await admin_client.post("/gas-volume-calc-types/", json=self._payload)
        ).status_code == 409

        listed = (await admin_client.get("/gas-volume-calc-types/")).json()
        assert len(listed) == 1
        gvct_id = listed[0]["id"]

        patched = await admin_client.patch(
            f"/gas-volume-calc-types/{gvct_id}", json={"type_name": "Флоутек-ТМ-2"}
        )
        assert patched.status_code == 202
        assert patched.json()["type_name"] == "Флоутек-ТМ-2"

        assert (
            await admin_client.delete(f"/gas-volume-calc-types/{gvct_id}")
        ).status_code == 204
        assert (await admin_client.get("/gas-volume-calc-types/")).json() == []

    async def test_viewer_can_read_not_write(self, viewer_client):
        assert (await viewer_client.get("/gas-volume-calc-types/")).status_code == 200
        resp = await viewer_client.post("/gas-volume-calc-types/", json=self._payload)
        assert resp.status_code == 403  # write blocked by the auth middleware
