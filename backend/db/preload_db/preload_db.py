import json

from backend.db.dao.edit_type_dao import EditTypeDao
from backend.db.dao.gas_volume_calc_type_dao import GasVolumeCalcTypeDao
from backend.db.dao.lumg_dao import LumgDao
from backend.db.dao.sys_type_dao import SysTypeDao
from backend.db.models import (
    EDIT_TYPE_CONSTRAINT,
    SYS_TYPE_CONSTRAINT,
    LumgCreate,
)
from backend.db.models.gas_volume_calc_type_model import GAS_VOLUME_CALC_TYPE_CONSTRAINT


async def preload_db():
    new_lumg = LumgCreate(name="ЗЛВУМГ")
    await LumgDao().create_item(new_lumg)
    path = "backend/db/preload_db/FLOWTYPE.json"
    with open(path, "r", encoding="utf8") as file:
        flow_type = json.load(file)["FLOWTYPE"]
    unique_type_id_calc = set([flow_dict["ID_TYPE"] for flow_dict in flow_type])
    instance_list = [
        {"type_id": flow_dict["ID_TYPE"], "type_name": flow_dict["TYPENAME"].strip()}
        for flow_dict in flow_type
    ]
    await GasVolumeCalcTypeDao().bulk_upsert(
        instance_list, GAS_VOLUME_CALC_TYPE_CONSTRAINT
    )
    type_id_dict = {
        type_id: await GasVolumeCalcTypeDao().get_by_type_id(type_id)
        for type_id in unique_type_id_calc
    }

    path = "backend/db/preload_db/EDITNAME.json"
    with open(path, "r", encoding="utf8") as file:
        flow_type = json.load(file)["EDITNAME"]
    instance_list = [
        {
            "edit_type_id": flow_dict["EDIT_ID"],
            "gas_volume_calc_type_id": type_id_dict[flow_dict["ID_TYPE"]],
            "edit_name": flow_dict["EDITNAME"].strip(),
        }
        for flow_dict in flow_type
    ]
    await EditTypeDao().bulk_upsert(instance_list, EDIT_TYPE_CONSTRAINT)

    path = "backend/db/preload_db/SYSNAME.json"
    with open(path, "r", encoding="utf8") as file:
        flow_type = json.load(file)["SYSNAME"]
    instance_list = [
        {
            "sys_type_id": flow_dict["SYS_ID"],
            "gas_volume_calc_type_id": type_id_dict[flow_dict["ID_TYPE"]],
            "sys_name": flow_dict["SYSNAME"].strip(),
        }
        for flow_dict in flow_type
    ]
    await SysTypeDao().bulk_upsert(instance_list, SYS_TYPE_CONSTRAINT)


if __name__ == "__main__":
    preload_db()
