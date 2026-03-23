import asyncio

from backend.db.dao.gas_volume_calc_dao import GasVolumeCalcDao
from backend.db.dao.gas_volume_calc_type_dao import GasVolumeCalcTypeDao
from backend.db.dao.line_dao import LineDao
from backend.db.engine import async_session_factory
from backend.hl_engine.data_classes.cfg_dataclass import (
    HeaderStruct,
    GisStruct,
    FlowStruct,
    LineStruct,
    GasVolumeCalcStruct,
)


class ConfigReader:
    def __init__(self, file):
        self.file = file

    def read(self) -> list[dict]:
        """Read all GIS (LUMG) entries from the CFG file.

        GIS entries are separated by variable-length path gaps, so after each
        complete GIS we scan forward for the next \\x04\\x00 marker.
        header.gis_num gives the total number of GIS entries.

        Returns a list of dicts:
          [{"gis_name": str, "flows": [{"name", "address", "type_id", "lines": [...]}]}]
        """
        with open(self.file, "rb") as cfg_file:
            raw = cfg_file.read()

        if len(raw) < HeaderStruct.size:
            return []
        header = HeaderStruct.unpack(raw[:HeaderStruct.size])
        if HeaderStruct.get_string_from_bytes(header.header) != "PssCfg":
            return []

        gis_count = header.gis_num  # actual count of GIS entries
        cursor = HeaderStruct.size
        gis_list = []
        marker = b'\x04\x00'

        for _ in range(gis_count):
            # Skip any gap (path string) between entries by finding next marker
            marker_pos = raw.find(marker, cursor)
            if marker_pos == -1 or marker_pos + GisStruct.size > len(raw):
                break
            cursor = marker_pos

            gis_struct = GisStruct.unpack(raw[cursor: cursor + GisStruct.size])
            lumg_name = GisStruct.get_string_from_bytes(gis_struct.gis_name)[
                : gis_struct.gis_name_length
            ]
            cursor += GisStruct.size

            flows = []
            ok = True
            for _ in range(gis_struct.flow_num):
                if cursor + FlowStruct.size > len(raw):
                    ok = False
                    break
                flow_struct = FlowStruct.unpack(raw[cursor: cursor + FlowStruct.size])
                flow_name = FlowStruct.get_string_from_bytes(flow_struct.flow_name)[
                    : flow_struct.flow_name_length
                ]
                cursor += FlowStruct.size

                lines = []
                for _ in range(flow_struct.line_num):
                    if cursor + LineStruct.size > len(raw):
                        ok = False
                        break
                    line_struct = LineStruct.unpack(raw[cursor: cursor + LineStruct.size])
                    line_name = LineStruct.get_string_from_bytes(line_struct.line_name)[
                        : line_struct.line_name_length
                    ]
                    lines.append({
                        "name": line_name,
                        "line": line_struct.line_num,
                        "meter": line_struct.meter_type,
                    })
                    cursor += LineStruct.size

                if cursor + GasVolumeCalcStruct.size > len(raw):
                    ok = False
                    break
                gvc_struct = GasVolumeCalcStruct.unpack(raw[cursor: cursor + GasVolumeCalcStruct.size])
                cursor += GasVolumeCalcStruct.size

                flows.append({
                    "name": flow_name,
                    "address": gvc_struct.flow_address,
                    "type_id": gvc_struct.flow_id,
                    "lines": lines,
                })

            gis_list.append({"gis_name": lumg_name, "flows": flows})
            if not ok:
                break

        return gis_list

    async def update_db(self):
        """Legacy: update using first GIS, matching LUMG by name."""
        gis_list = self.read()
        if not gis_list:
            return
        gis = gis_list[0]
        async with async_session_factory() as session:
            from backend.db.dao.lumg_dao import LumgDao
            lumg_dao = LumgDao(session=session)
            lumg_db = await lumg_dao.update_if_exist(gis["gis_name"])
            lumg_id = lumg_db.id if lumg_db else 1
            await self._update_flows(session, gis["flows"], lumg_id)

    async def update_db_with_mapping(self, mapping: dict[str, int]):
        """Update names using explicit gis_name → lumg_id mapping."""
        gis_list = self.read()
        async with async_session_factory() as session:
            for gis in gis_list:
                lumg_id = mapping.get(gis["gis_name"])
                if lumg_id is None:
                    continue
                await self._update_flows(session, gis["flows"], lumg_id)

    async def _update_flows(self, session, flows: list[dict], lumg_id: int):
        gas_volume_calc_dao = GasVolumeCalcDao(session=session)
        gas_volume_type_dao = GasVolumeCalcTypeDao(session=session)
        line_dao = LineDao(session=session)

        for flow in flows:
            type_id = await gas_volume_type_dao.get_or_create_by_type_id(flow["type_id"])
            gas_volume_db = await gas_volume_calc_dao.update_if_exists(
                lumg_id=lumg_id,
                address=flow["address"],
                type_id=type_id,
                name=flow["name"],
            )
            if not gas_volume_db:
                continue
            for line in flow["lines"]:
                await line_dao.update_if_exists(
                    gas_volume_calc_id=gas_volume_db.id,
                    line=line["line"],
                    name=line["name"],
                    meter=line["meter"],
                )


if __name__ == "__main__":
    asyncio.run(ConfigReader(file="backend/db/preload_db/ask.CFG").update_db())
