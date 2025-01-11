import json

from backend.db.dao.edit_type_dao import EditTypeDao
from backend.db.dao.gas_volume_calc_type_dao import GasVolumeCalcTypeDao
from backend.db.dao.sys_type_dao import SysTypeDao
from backend.db.models import (
    GasVolumeCalcTypeCreate,
    EditTypeCreate,
    EDIT_TYPE_CONSTRAINT,
    SysTypeCreate,
    SYS_TYPE_CONSTRAINT,
)
from backend.db.models.gas_volume_calc_type_model import GAS_VOLUME_CALC_TYPE_CONSTRAINT

if __name__ == "__main__":
    path = "FLOWTYPE.json"
    with open(path, "r", encoding="utf8") as file:
        flow_type = json.load(file)["FLOWTYPE"]
    instance_list = [
        GasVolumeCalcTypeCreate(
            type_id=flow_dict["ID_TYPE"], type_name=flow_dict["TYPENAME"]
        )
        for flow_dict in flow_type
    ]
    GasVolumeCalcTypeDao().bulk_upsert(instance_list, GAS_VOLUME_CALC_TYPE_CONSTRAINT)

    path = "EDITNAME.json"
    with open(path, "r", encoding="utf8") as file:
        flow_type = json.load(file)["EDITNAME"]
    instance_list = [
        EditTypeCreate(
            edit_type_id=flow_dict["EDIT_ID"],
            gas_volume_calc_type_id=flow_dict["ID_TYPE"],
            edit_name=flow_dict["EDITNAME"],
        )
        for flow_dict in flow_type
    ]
    EditTypeDao().bulk_upsert(instance_list, EDIT_TYPE_CONSTRAINT)

    path = "SYSNAME.json"
    with open(path, "r", encoding="utf8") as file:
        flow_type = json.load(file)["SYSNAME"]
    instance_list = [
        SysTypeCreate(
            sys_type_id=flow_dict["SYS_ID"],
            gas_volume_calc_type_id=flow_dict["ID_TYPE"],
            sys_name=flow_dict["SYSNAME"],
        )
        for flow_dict in flow_type
    ]
    SysTypeDao().bulk_upsert(instance_list, SYS_TYPE_CONSTRAINT)
