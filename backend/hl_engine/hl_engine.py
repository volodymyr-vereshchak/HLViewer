import os
import glob

from datetime import datetime, date
from struct import calcsize, unpack
from collections import namedtuple

from backend.db.dao.custom_exceptions import DatabaseNoDataError
from backend.db.dao.gas_volume_calc_dao import GasVolumeCalcDao
from backend.db.models import DailyArchiveCreate, HourlyArchiveCreate
from utils.logger import logger_setup
from utils.math_utils import round_decimal


class Hostlib:
    def __init__(self, at: int = 0, path: str = "./") -> None:
        self.logger = logger_setup("backend")
        self.path = path
        self.day_mask = "S*R*D.*"
        self.hour_mask = "S*R*R.*"
        self.DayStruct = namedtuple(
            'DayStruct',
            "month day year volume unknown w_volume_dp pressure temperature density",
        )
        self.HourStruct = namedtuple(
            "HourStruct",
            "month day year hour minutes volume unknown w_volume_dp pressure temperature density"
        )
        self.day_struct = "=bbbffffff"
        self.hour_struct = "=bbbbbffffff"
        self.at = at

    @staticmethod
    def find_files_by_mask(path: str, mask: str) -> list[str]:
        file_path = os.path.join(path, mask)
        files = glob.glob(file_path)
        return files

    @staticmethod
    def get_params_from_file_name(path_to_file: str) -> dict:
        filename = os.path.basename(path_to_file)
        address = int(filename[1:4])
        line = int(filename[5:6])
        return {"address": address, "line": line}

    def read_daily_archive(self) -> list:
        files = self.find_files_by_mask(self.path, self.day_mask)
        daily_archive_list = []
        for file in files:
            flow_params = self.get_params_from_file_name(file)
            with open(file, "rb") as day_file:
                while True:
                    try:
                        data = day_file.read(calcsize(self.day_struct))
                        if not data:
                            break
                        day_data = self.DayStruct(*unpack(self.day_struct, data))
                        date_period = date(day_data.year + 2000, day_data.month, day_data.day)
                        day_dict = day_data._asdict()
                        day_dict = round_decimal(day_dict)
                        day_dict["period"] = date_period
                        day_dict["tech"] = flow_params["address"]
                        day_dict["line"] = flow_params["line"]
                        gas_volume_calc_id = GasVolumeCalcDao().get_id_by_address(flow_params['address'])
                        day_dict["gas_vol_calc_id"] = gas_volume_calc_id
                        daily_archive = DailyArchiveCreate(**day_dict)
                        daily_archive_list.append(daily_archive)
                    except DatabaseNoDataError as e:
                        self.logger.debug(e)
                        continue
        return daily_archive_list

    def read_hourly_archive(self) -> list:
        files = self.find_files_by_mask(self.path, self.hour_mask)
        hourly_archive_list = []
        for file in files:
            flow_params = self.get_params_from_file_name(file)
            with open(file, "rb") as hour_file:
                while True:
                    try:
                        data = hour_file.read(calcsize(self.hour_struct))
                        if not data:
                            break
                        hour_data = self.HourStruct(*unpack(self.hour_struct, data))
                        datetime_period = datetime(hour_data.year + 2000, hour_data.month, hour_data.day, hour_data.hour, hour_data.minutes)
                        hour_dict = hour_data._asdict()
                        hour_dict = round_decimal(hour_dict)
                        hour_dict["period"] = datetime_period
                        hour_dict['tech'] = flow_params['address']
                        hour_dict['line'] = flow_params['line']
                        gas_volume_calc_id = GasVolumeCalcDao().get_id_by_address(flow_params['address'])
                        hour_dict["gas_vol_calc_id"] = gas_volume_calc_id
                        hourly_archive = HourlyArchiveCreate(**hour_dict)
                        hourly_archive_list.append(hourly_archive)
                    except DatabaseNoDataError as e:
                        self.logger.debug(e)
                        continue
        return hourly_archive_list


if __name__ == "__main__":
    path_dir = "D:/Projects/HLViewer/HLViewer/develop_data/11Листопад/Zaporizgaz_2024_11_29_8/Zaporizgaz/56ZOPZAP4003301T"
    print(Hostlib(path=path_dir).read_daily_archive())
