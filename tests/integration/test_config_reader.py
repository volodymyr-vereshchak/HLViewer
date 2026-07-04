"""ConfigReader (ask.cfg) tests: binary parsing of the ASK-1 config layout and
update_db_with_mapping — which only RENAMES existing calcs/lines (creation
happens during archive ingest, see test_hl_engine_ingest)."""

import struct

from sqlmodel import select

from backend.db.engine import async_session_factory
from backend.db.models import GasVolumeCalc, GasVolumeCalcType, Line
from backend.hl_engine.config_reader import ConfigReader


def _pad32(name: str) -> bytes:
    return name.encode("ascii").ljust(32, b"\x00")


def _gis_block(
    gis_name: str,
    flow_name: str = "GRS-1",
    address: int = 12,
    flow_type_id: int = 5,
    line_name: str = "Line-A",
    line_num: int = 1,
    meter: int = 1,
) -> bytes:
    gis = struct.pack("=2sB32sBBB", b"\x04\x00", len(gis_name), _pad32(gis_name), 0, 1, 0)
    flow = struct.pack("=2sB32sBBB", b"\x04\x00", len(flow_name), _pad32(flow_name), 0, 1, 0)
    line = struct.pack(
        "=2sB32sBB124sB3s",
        b"\x04\x00", len(line_name), _pad32(line_name), 0, line_num,
        b"\x00" * 124, meter, b"\x00" * 3,
    )
    gvc = struct.pack("=BBBB234s", address, 0, 7, flow_type_id, b"\x00" * 234)
    archive_path = b"\x07" + b"C:\\arch"
    return gis + flow + line + gvc + archive_path


def _cfg_bytes(*gis_blocks: bytes) -> bytes:
    header = struct.pack("=6s42s", b"PssCfg", b"\x00" * 42)
    group = struct.pack("=B32sBBB", 5, _pad32("GROUP"), 0, len(gis_blocks), 0)
    return header + group + b"".join(gis_blocks)


def _write_cfg(tmp_path, data: bytes) -> str:
    path = tmp_path / "ask.CFG"
    path.write_bytes(data)
    return str(path)


class TestRead:
    def test_parses_structure(self, tmp_path):
        cfg = _write_cfg(tmp_path, _cfg_bytes(_gis_block("TESTGIS")))
        result = ConfigReader(cfg).read()
        assert len(result) == 1
        gis = result[0]
        assert gis["gis_name"] == "TESTGIS"
        assert len(gis["flows"]) == 1
        flow = gis["flows"][0]
        assert flow["name"] == "GRS-1"
        assert flow["address"] == 12
        assert flow["type_id"] == 5
        assert flow["lines"] == [{"name": "Line-A", "line": 1, "meter": True}]

    def test_parses_multiple_gis(self, tmp_path):
        cfg = _write_cfg(
            tmp_path,
            _cfg_bytes(_gis_block("GISONE"), _gis_block("GISTWO", address=34)),
        )
        result = ConfigReader(cfg).read()
        assert [g["gis_name"] for g in result] == ["GISONE", "GISTWO"]
        assert result[1]["flows"][0]["address"] == 34

    def test_rejects_wrong_signature(self, tmp_path):
        data = _cfg_bytes(_gis_block("TESTGIS"))
        cfg = _write_cfg(tmp_path, b"BadSig" + data[6:])
        assert ConfigReader(cfg).read() == []

    def test_rejects_truncated_file(self, tmp_path):
        cfg = _write_cfg(tmp_path, b"PssCfg\x00\x00")
        assert ConfigReader(cfg).read() == []


class TestUpdateDbWithMapping:
    async def test_renames_existing_calc_and_line(self, seed_topology, tmp_path):
        # known calc type so the flow's type_id byte resolves to a DB row
        async with async_session_factory() as session:
            calc_type = GasVolumeCalcType(type_id=5, type_name="Тип 5")
            session.add(calc_type)
            await session.commit()
            await session.refresh(calc_type)

        cfg = _write_cfg(tmp_path, _cfg_bytes(_gis_block("TESTGIS")))
        await ConfigReader(cfg).update_db_with_mapping(
            {"TESTGIS": seed_topology["lumg"]}
        )

        async with async_session_factory() as session:
            calc = (
                await session.execute(
                    select(GasVolumeCalc).where(
                        GasVolumeCalc.id == seed_topology["calc"]
                    )
                )
            ).scalar_one()
            line = (
                await session.execute(
                    select(Line).where(Line.id == seed_topology["line1"])
                )
            ).scalar_one()

        assert calc.name == "GRS-1"  # a12 → human-readable name from ask.cfg
        assert calc.type_id == calc_type.id
        assert line.name == "Line-A"  # l1 → name from ask.cfg
        assert line.meter is True

    async def test_unmapped_gis_skipped(self, seed_topology, tmp_path):
        cfg = _write_cfg(tmp_path, _cfg_bytes(_gis_block("UNKNOWNGIS")))
        await ConfigReader(cfg).update_db_with_mapping({})  # nothing mapped

        async with async_session_factory() as session:
            calc = (
                await session.execute(
                    select(GasVolumeCalc).where(
                        GasVolumeCalc.id == seed_topology["calc"]
                    )
                )
            ).scalar_one()
        assert calc.name == "a12"  # untouched

    async def test_missing_calc_not_created(self, seed_topology, tmp_path):
        # flow address 99 has no calc in DB → update_if_exists is a no-op
        cfg = _write_cfg(tmp_path, _cfg_bytes(_gis_block("TESTGIS", address=99)))
        await ConfigReader(cfg).update_db_with_mapping(
            {"TESTGIS": seed_topology["lumg"]}
        )

        async with async_session_factory() as session:
            calcs = (await session.execute(select(GasVolumeCalc))).scalars().all()
        assert len(calcs) == 1  # only the seeded one; nothing auto-created
