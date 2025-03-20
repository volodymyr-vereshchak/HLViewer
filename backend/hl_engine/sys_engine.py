from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.dao.gas_volume_calc_dao import GasVolumeCalcDao
from backend.db.dao.gas_volume_calc_type_dao import GasVolumeCalcTypeDao
from backend.db.dao.line_dao import LineDao
from backend.db.dao.sys_type_dao import SysTypeDao
from backend.db.models import SysArchiveCreate, SysTypeCreate
from backend.hl_engine.data_classes.sys_dataclass import SysStruct
from backend.hl_engine.hl_engine import Hostlib
from utils.files_utils import find_files_by_mask, read_archive_file


class SysEngine(Hostlib):

    def __init__(
        self,
        session: AsyncSession,
        path: str = "./",
        chunk_size: int = 900,
        lumg_id: int = 1,
    ) -> None:
        super().__init__(path, chunk_size)
        self.sys_mask = "S*R*A.*"
        self.sys_struct = SysStruct
        self.create_class = SysArchiveCreate
        self.lumg_id = lumg_id
        self.session = session

    async def read(self):
        files = find_files_by_mask(self.path, self.sys_mask)
        archive_dict_list = []
        gas_volume_dao = GasVolumeCalcDao(session=self.session)
        sys_type_dao = SysTypeDao(session=self.session)
        line_dao = LineDao(session=self.session)
        for file in files:
            flow_params = self.get_params_from_file_name(file)
            gas_volume_calc = await gas_volume_dao.get_or_create(
                address=flow_params["address"], lumg_id=self.lumg_id
            )
            gas_volume_calc_id = gas_volume_calc.id
            gas_volume_calc_type_id = await GasVolumeCalcTypeDao(session=self.session).get_by_type_id(gas_volume_calc.type_id)

            gas_volume_line = await line_dao.get_or_create(
                gas_volume_calc_id, flow_params["line"]
            )

            line_id = gas_volume_line.id

            sys_list = await sys_type_dao.get_by_gas_volume_type_id(
                gas_volume_calc_type_id
            )
            sys_dict = {instance.sys_type_id: instance.id for instance in sys_list}

            read_archive_gen = read_archive_file(file, self.sys_struct)
            while True:
                try:
                    file_dict = next(read_archive_gen)
                    datetime_period = datetime(
                        file_dict["year"] + 2000,
                        file_dict["month"],
                        file_dict["day"],
                        file_dict["hour"],
                        file_dict["minutes"],
                        file_dict["seconds"],
                    )

                    if file_dict.get("sys_type_id") is not None:
                        try:
                            file_dict["sys_type_id"] = sys_dict[
                                file_dict["sys_type_id"]
                            ]
                        except KeyError:
                            new_sys = SysTypeCreate(
                                sys_type_id=file_dict["sys_type_id"],
                                gas_volume_calc_type_id=gas_volume_calc_type_id,
                                sys_name=f"Неизвестный код {file_dict['sys_type_id']}",
                            )
                            new_item = await sys_type_dao.create_item(new_sys)
                            file_dict["sys_type_id"] = new_item.id
                            sys_dict[file_dict["sys_type_id"]] = new_item.id
                            self.logger.debug(
                                f"No sys type with this id: {file_dict['sys_type_id']}! Created new!"
                            )

                    file_dict["period"] = datetime_period
                    file_dict["line_id"] = line_id
                    archive_dict = {
                        key: value
                        for key, value in file_dict.items()
                        if key in self.create_class.model_fields
                    }
                    archive_dict_list.append(archive_dict)
                    if len(archive_dict_list) == self.chunk_size:
                        yield archive_dict_list
                        archive_dict_list = []

                except StopIteration:
                    break

                except ValueError as e:
                    self.logger.debug(e)

        if archive_dict_list:
            yield archive_dict_list
