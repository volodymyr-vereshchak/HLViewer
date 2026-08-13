"""The event-type dictionaries and their JSON files.

These three files are the only way an edit made in the admin panel survives a
restart — the backend reloads them into the database on every start — and the
only way one reaches the offline server. So both directions are covered here,
plus the one mistake that would be unrecoverable: a "reload the dictionaries"
button reaching gas_vol_calc_type, whose deletion cascades to every line and
its whole archive.
"""

import json

import pytest
import pytest_asyncio

import backend.db.preload_db.event_types_json as et
from backend.db.engine import async_session_factory
from backend.db.models.edit_type_model import EditType
from backend.db.models.gas_volume_calc_type_model import GasVolumeCalcType
from backend.db.models.sys_type_model import SysType


@pytest.fixture
def preload_files(tmp_path, monkeypatch):
    """Point the module at throwaway files. The repo's own SYSNAME.json is
    committed data — a test must never read it, and certainly never rewrite it."""
    paths = {
        "FLOWTYPE_PATH": tmp_path / "FLOWTYPE.json",
        "SYSNAME_PATH": tmp_path / "SYSNAME.json",
        "EDITNAME_PATH": tmp_path / "EDITNAME.json",
    }
    for attr, target in paths.items():
        monkeypatch.setattr(et, attr, target)

    def write(flowtype=None, sysname=None, editname=None) -> None:
        paths["FLOWTYPE_PATH"].write_text(
            json.dumps({"FLOWTYPE": flowtype or []}, ensure_ascii=False), encoding="utf-8"
        )
        paths["SYSNAME_PATH"].write_text(
            json.dumps({"SYSNAME": sysname or []}, ensure_ascii=False), encoding="utf-8"
        )
        paths["EDITNAME_PATH"].write_text(
            json.dumps({"EDITNAME": editname or []}, ensure_ascii=False), encoding="utf-8"
        )

    write.paths = paths
    return write


@pytest_asyncio.fixture
async def dictionaries(clean_db) -> None:
    """One calculator type with one accident and one change type."""
    async with async_session_factory() as session:
        session.add(GasVolumeCalcType(type_id=9, type_name="Флоутек"))
        session.add(SysType(sys_type_id=2, gas_volume_calc_type_id=9, sys_name="Аварія"))
        session.add(EditType(edit_type_id=1, gas_volume_calc_type_id=9, edit_name="Густина"))
        await session.commit()


async def _rows(model) -> list:
    from sqlmodel import select

    async with async_session_factory() as session:
        return (await session.execute(select(model))).scalars().all()


class TestExport:
    async def test_writes_the_shape_the_loader_reads(self, dictionaries, preload_files):
        preload_files()
        async with async_session_factory() as session:
            counts = await et.export_event_types(session)

        assert (counts.flowtype, counts.sysname, counts.editname) == (1, 1, 1)
        sysname = json.loads(preload_files.paths["SYSNAME_PATH"].read_text(encoding="utf-8"))
        assert sysname == {
            "SYSNAME": [{"ID_TYPE": 9, "SYS_ID": 2, "SYSNAME": "Аварія"}]
        }

    async def test_round_trip_restores_a_wiped_database(self, dictionaries, preload_files):
        """Export then reload must give back exactly what was there — this is
        the whole transfer to the offline server in one assertion."""
        preload_files()
        async with async_session_factory() as session:
            await et.export_event_types(session)
            await et.import_event_types(session, force=True)

        sys_types = await _rows(SysType)
        edit_types = await _rows(EditType)
        assert [(s.sys_type_id, s.gas_volume_calc_type_id, s.sys_name) for s in sys_types] == [
            (2, 9, "Аварія")
        ]
        assert [(e.edit_type_id, e.gas_volume_calc_type_id, e.edit_name) for e in edit_types] == [
            (1, 9, "Густина")
        ]


class TestImport:
    async def test_merge_renames_by_code_pair_and_adds_missing(self, dictionaries, preload_files):
        preload_files(
            flowtype=[{"ID_TYPE": 9, "TYPENAME": "Флоутек"}],
            sysname=[
                {"ID_TYPE": 9, "SYS_ID": 2, "SYSNAME": "Нова назва  "},
                {"ID_TYPE": 9, "SYS_ID": 3, "SYSNAME": "Нова подія"},
            ],
        )
        async with async_session_factory() as session:
            await et.import_event_types(session)

        by_code = {s.sys_type_id: s.sys_name for s in await _rows(SysType)}
        # Renamed in place: same (code, calculator type), name from the file.
        # Trailing whitespace of the original ASK export is stripped.
        assert by_code == {2: "Нова назва", 3: "Нова подія"}

    async def test_merge_keeps_a_row_the_file_does_not_have(self, dictionaries, preload_files):
        preload_files(sysname=[{"ID_TYPE": 9, "SYS_ID": 3, "SYSNAME": "Нова подія"}])
        async with async_session_factory() as session:
            await et.import_event_types(session)

        assert {s.sys_type_id for s in await _rows(SysType)} == {2, 3}

    async def test_force_drops_a_row_the_file_does_not_have(self, dictionaries, preload_files):
        preload_files(sysname=[{"ID_TYPE": 9, "SYS_ID": 3, "SYSNAME": "Нова подія"}])
        async with async_session_factory() as session:
            counts = await et.import_event_types(session, force=True)

        assert counts.wiped is True
        assert {s.sys_type_id for s in await _rows(SysType)} == {3}
        # The change dictionary is wiped too — the file listed none.
        assert await _rows(EditType) == []

    async def test_force_never_touches_the_calculator_types(self, dictionaries, preload_files):
        """Deleting a gas_vol_calc_type cascades to its lines and their entire
        archive. Reloading the event dictionaries must not be able to do that,
        even with an empty FLOWTYPE.json."""
        preload_files()
        async with async_session_factory() as session:
            await et.import_event_types(session, force=True)

        assert [c.type_id for c in await _rows(GasVolumeCalcType)] == [9]

    async def test_a_missing_file_leaves_its_dictionary_alone(self, dictionaries, preload_files):
        preload_files()
        preload_files.paths["SYSNAME_PATH"].unlink()
        async with async_session_factory() as session:
            counts = await et.import_event_types(session)

        assert counts.sysname == 0
        assert {s.sys_type_id for s in await _rows(SysType)} == {2}


class TestEndpoints:
    async def test_export_then_preload_over_http(self, admin_client, dictionaries, preload_files):
        preload_files()
        assert (
            await admin_client.post("/gas-volume-calc-types/export-preload")
        ).json() == {"ok": True, "exported": {"flowtype": 1, "sysname": 1, "editname": 1}}

        resp = await admin_client.post("/gas-volume-calc-types/preload", params={"force": True})
        assert resp.json()["wiped"] is True
        assert {s.sys_type_id for s in await _rows(SysType)} == {2}

    async def test_viewer_cannot_move_the_files(self, viewer_client, preload_files):
        preload_files()
        assert (
            await viewer_client.post("/gas-volume-calc-types/export-preload")
        ).status_code == 403
        assert (await viewer_client.post("/gas-volume-calc-types/preload")).status_code == 403
