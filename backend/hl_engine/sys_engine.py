from collections import namedtuple
from datetime import datetime

from backend.db.dao.gas_volume_calc_dao import GasVolumeCalcDao
from backend.db.dao.sys_type_dao import SysTypeDao
from backend.db.models import SysArchiveCreate, SysTypeCreate
from backend.hl_engine.hl_engine import Hostlib
from utils.files_utils import find_files_by_mask, read_archive_file


class SysEngine(Hostlib):

    def __init__(self, path: str = "./", chunk_size: int = 900) -> None:
        super().__init__(path, chunk_size)
        self.sys_mask = "S*R*A.*"
        self.SysStruct = namedtuple(
            "SysStruct",
            "month day year hour minutes seconds sys_type_id unknown standard_volume",
            # TODO check struct of sys archive unknown
        )
        self.sys_struct = "=BBBBBBHBf"
        self.create_class = SysArchiveCreate

    def read(self):
        files = find_files_by_mask(self.path, self.sys_mask)
        archive_dict_list = []
        gas_volume_dao = GasVolumeCalcDao()
        sys_type_dao = SysTypeDao()
        for file in files:
            flow_params = self.get_params_from_file_name(file)
            gas_volume_calc = (
                gas_volume_dao.get_flow_calc_by_address_and_line_or_create(
                    flow_params["address"], flow_params["line"]
                )
            )
            gas_volume_calc_id = gas_volume_calc.id
            gas_volume_calc_type_id = gas_volume_calc.type_id

            sys_list = sys_type_dao.get_by_gas_volume_type_id(gas_volume_calc_type_id)
            sys_dict = {instance.sys_type_id: instance.id for instance in sys_list}

            read_archive_gen = read_archive_file(file, self.sys_struct, self.SysStruct)
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
                            new_item = sys_type_dao.create_item(new_sys)
                            file_dict["sys_type_id"] = new_item.id
                            sys_dict[file_dict["sys_type_id"]] = new_item.id
                            self.logger.debug(
                                f"No sys type with this id: {file_dict['sys_type_id']}! Created new!"
                            )

                    file_dict["period"] = datetime_period
                    file_dict["gas_vol_calc_id"] = gas_volume_calc_id
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
