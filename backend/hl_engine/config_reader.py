from backend.db.dao.gas_volume_calc_dao import GasVolumeCalcDao
from backend.db.dao.gas_volume_calc_type_dao import GasVolumeCalcTypeDao
from backend.db.dao.line_dao import LineDao
from backend.db.dao.lumg_dao import LumgDao
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

    def read(self):
        with open(self.file, "rb") as cfg_file:
            data = cfg_file.read(HeaderStruct.size)
            header = HeaderStruct.unpack(data)
            if header.get_string_from_bytes(header.header) == "PssCfg":
                gis_data = cfg_file.read(GisStruct.size)
                gis_struct = GisStruct.unpack(gis_data)
                lumg_name = gis_struct.get_string_from_bytes(gis_struct.gis_name)[
                    : gis_struct.gis_name_length
                ]
                flow_num = gis_struct.flow_num
                flows = []
                for _ in range(flow_num):
                    flow_data = cfg_file.read(FlowStruct.size)
                    flow_struct = FlowStruct.unpack(flow_data)
                    lime_num = flow_struct.line_num
                    flow_name = flow_struct.get_string_from_bytes(
                        flow_struct.flow_name
                    )[: flow_struct.flow_name_length]
                    lines = []
                    for _ in range(lime_num):
                        line_data = cfg_file.read(LineStruct.size)
                        line_struct = LineStruct.unpack(line_data)
                        line_name = line_struct.get_string_from_bytes(
                            line_struct.line_name
                        )[: line_struct.line_name_length]
                        line_number = line_struct.line_num
                        meter_type = line_struct.meter_type
                        lines.append(
                            {
                                "name": line_name,
                                "line": line_number,
                                "meter": meter_type,
                            }
                        )
                    gas_volume_calc_data = cfg_file.read(GasVolumeCalcStruct.size)
                    gas_volume_calc_struct = GasVolumeCalcStruct.unpack(
                        gas_volume_calc_data
                    )
                    address = gas_volume_calc_struct.flow_address
                    flow_type = gas_volume_calc_struct.flow_id
                    c_time = 7
                    flows.append(
                        {
                            "name": flow_name,
                            "type_id": flow_type,
                            "address": address,
                            "lines": lines,
                            "c_time": c_time,
                        }
                    )
                result = {"lumg_name": lumg_name, "flows": flows}
                return result

    async def update_db(self):
        data = self.read()
        lumg_dao = LumgDao()
        gas_volume_calc_dao = GasVolumeCalcDao()
        gas_volume_type_dao = GasVolumeCalcTypeDao()
        line_dao = LineDao()
        lumg_name = data["lumg_name"]
        lumg_db = await lumg_dao.update_if_exist(lumg_name)
        lumg_id = 1  # lumg_db.id

        for flow in data["flows"]:
            lines = flow.pop("lines")
            type_id = await gas_volume_type_dao.get_by_type_id(flow["type_id"])
            if type_id:
                flow["type_id"] = type_id
                gas_volume_db = await gas_volume_calc_dao.update_if_exists(
                    lumg_id=lumg_id, **flow
                )
                if gas_volume_db:
                    gas_volume_id = gas_volume_db.id

                    for line in lines:
                        line_db = await line_dao.update_if_exists(
                            gas_volume_calc_id=gas_volume_id, **line
                        )


if __name__ == "__main__":
    ConfigReader(file="backend/db/preload_db/ask.CFG").update_db()
