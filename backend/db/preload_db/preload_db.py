import asyncio
import json
from datetime import datetime

from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.dao.edit_type_dao import EditTypeDao
from backend.db.dao.gas_volume_calc_type_dao import GasVolumeCalcTypeDao
from backend.db.dao.lumg_dao import LumgDao
from backend.db.dao.sys_type_dao import SysTypeDao
from backend.db.engine import async_session_factory
from backend.db.models import (
    EDIT_TYPE_CONSTRAINT,
    SYS_TYPE_CONSTRAINT,
    LumgCreate,
)
from backend.db.models.gas_volume_calc_type_model import GAS_VOLUME_CALC_TYPE_CONSTRAINT


async def preload_db():
    async with async_session_factory() as session:
        try:
            new_lumg = LumgCreate(name="ЗЛВУМГ")
            await LumgDao(session=session).create_item(new_lumg)
        except DatabaseIntegrityError as e:
            pass
        path = "backend/db/preload_db/FLOWTYPE.json"
        with open(path, "r", encoding="utf8") as file:
            flow_type = json.load(file)["FLOWTYPE"]
        unique_type_id_calc = set([flow_dict["ID_TYPE"] for flow_dict in flow_type])
        instance_list = [
            {
                "type_id": flow_dict["ID_TYPE"],
                "type_name": flow_dict["TYPENAME"].strip(),
                "updated_at": datetime.now()
            }
            for flow_dict in flow_type
        ]
        await GasVolumeCalcTypeDao(session=session).bulk_upsert_with_update(
            instance_list, GAS_VOLUME_CALC_TYPE_CONSTRAINT
        )
        type_id_dict = {
            type_id: await GasVolumeCalcTypeDao(session=session).get_by_type_id(type_id)
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
                "updated_at": datetime.now(),
            }
            for flow_dict in flow_type
        ]
        await EditTypeDao(session=session).bulk_upsert_with_update(
            instance_list, EDIT_TYPE_CONSTRAINT
        )

        path = "backend/db/preload_db/SYSNAME.json"
        with open(path, "r", encoding="utf8") as file:
            flow_type = json.load(file)["SYSNAME"]
        instance_list = [
            {
                "sys_type_id": flow_dict["SYS_ID"],
                "gas_volume_calc_type_id": type_id_dict[flow_dict["ID_TYPE"]],
                "sys_name": flow_dict["SYSNAME"].strip(),
                "updated_at": datetime.now(),
            }
            for flow_dict in flow_type
        ]
        batch_size = 1000

        for i in range(0, len(instance_list), batch_size):
            batch = instance_list[i : i + batch_size]
            await SysTypeDao(session=session).bulk_upsert_with_update(batch, SYS_TYPE_CONSTRAINT)


if __name__ == "__main__":
    asyncio.run(preload_db())
