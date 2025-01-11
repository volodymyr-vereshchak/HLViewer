import os
import glob
import shutil
import zipfile

from datetime import datetime, date
from struct import calcsize, unpack
from collections import namedtuple

from backend.db.dao.gas_volume_calc_dao import GasVolumeCalcDao
from backend.db.models import (
    DailyArchiveCreate,
    HourlyArchiveCreate,
    GasVolumeCalcCreate,
    EditArchiveCreate,
    SysArchiveCreate,
)
from utils.files_utils import find_files_by_mask
from utils.logger import logger_setup
from utils.math_utils import round_decimal


class Hostlib:
    def __init__(self, at: int = 0, path: str = "./") -> None:
        self.logger = logger_setup("backend")
        self.path = path
        self.day_mask = "S*R*D.*"
        self.hour_mask = "S*R*R.*"
        self.edit_mask = "S*R*U.*"
        self.sys_mask = "S*R*A.*"
        self.DayStruct = namedtuple(
            "DayStruct",
            "month day year volume unknown w_volume_dp pressure temperature density",
        )
        self.HourStruct = namedtuple(
            "HourStruct",
            "month day year hour minutes volume unknown w_volume_dp pressure temperature density",
        )
        self.EditStruct = namedtuple(
            "EditStruct",
            "month day year hour minutes seconds edit_id line old_value new_value",
        )
        self.SysStruct = namedtuple(
            "SysStruct",
            "month day year hour minutes seconds sys_type_id line standard_volume",
        )

        self.day_struct = "=bbbffffff"
        self.hour_struct = "=bbbbbffffff"
        self.edit_struct = "=bbbbbbbbii"
        self.sys_struct = "=bbbbbbhbf"
        self.at = at

    @staticmethod
    def get_params_from_file_name(path_to_file: str) -> dict:
        filename = os.path.basename(path_to_file)
        address = int(filename[1:4])
        line = int(filename[5:6])
        return {"address": address, "line": line}

    def read_archive(self, mask, file_struct, struct_tuple, create_class):
        files = find_files_by_mask(self.path, mask)
        archive_list = []
        gas_volume_dao = GasVolumeCalcDao()
        for file in files:
            flow_params = self.get_params_from_file_name(file)
            gas_volume_calc_id = gas_volume_dao.get_id_by_address_and_line(
                flow_params["address"], flow_params["line"]
            )
            if not gas_volume_calc_id:
                self.logger.debug(
                    f"No gas volume calc with this address: {flow_params['address']} line: {flow_params['line']}! Created new!"
                )
                gvc = GasVolumeCalcCreate(
                    address=flow_params["address"],
                    line=flow_params["line"],
                    meter=False,
                    name=f"a{flow_params['address']}_l{flow_params['line']}",
                    c_time=7,
                    lumg_id=1,
                    type_id=1,
                )
                gas_volume_calc = gas_volume_dao.create_item(gvc)
                gas_volume_calc_id = gas_volume_calc.id
            with open(file, "rb") as archive_file:
                while True:
                    try:
                        data = archive_file.read(calcsize(file_struct))
                        if not data:
                            break
                        file_data = struct_tuple(*unpack(file_struct, data))
                        if "seconds" in struct_tuple._fields:
                            datetime_period = datetime(
                                file_data.year + 2000,
                                file_data.month,
                                file_data.day,
                                file_data.hour,
                                file_data.minutes,
                                file_data.seconds,
                            )
                        elif "minutes" in struct_tuple._fields:
                            datetime_period = datetime(
                                file_data.year + 2000,
                                file_data.month,
                                file_data.day,
                                file_data.hour,
                                file_data.minutes,
                            )
                        else:
                            datetime_period = date(
                                file_data.year + 2000, file_data.month, file_data.day
                            )
                        file_dict = file_data._asdict()
                        file_dict = round_decimal(file_dict)
                        file_dict["period"] = datetime_period
                        file_dict["tech"] = flow_params["address"]
                        file_dict["line"] = flow_params["line"]
                        file_dict["gas_vol_calc_id"] = gas_volume_calc_id
                        archive = create_class(**file_dict)
                        archive_list.append(archive)
                    except ValueError as e:
                        self.logger.debug(e)
        return archive_list

    def read_daily_archive(self) -> list:
        daily_archive_list = self.read_archive(
            self.day_mask, self.day_struct, self.DayStruct, DailyArchiveCreate
        )
        return daily_archive_list

    def read_hourly_archive(self) -> list:
        hourly_archive_list = self.read_archive(
            self.hour_mask, self.hour_struct, self.HourStruct, HourlyArchiveCreate
        )
        return hourly_archive_list

    def read_edit_archive(self) -> list:
        edit_archive_list = self.read_archive(
            self.edit_mask, self.edit_struct, self.EditStruct, EditArchiveCreate
        )
        return edit_archive_list

    def read_sys_archive(self) -> list:
        sys_archive_list = self.read_archive(
            self.sys_mask, self.sys_struct, self.SysStruct, SysArchiveCreate
        )
        return sys_archive_list


if __name__ == "__main__":
    path_dir = "D:/Projects/HLViewer/HLViewer/develop_data/11Листопад/Zaporizgaz_2024_11_29_8/Zaporizgaz/56ZOPZAP4003301T"
    print(Hostlib(path=path_dir).read_daily_archive())
