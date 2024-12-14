import os
import glob
import pandas as pd
from datetime import datetime
from struct import calcsize, unpack
from collections import namedtuple


class Hostlib:
    def __init__(self, at: int = 0) -> None:
        self.day_mask = "S*R*D.*"
        self.hour_mask = "S*R*R.*"
        self.DayStruct = namedtuple(
            'DayStruct',
            "month day year volume unknown work_volume_dp pressure temperature density",
        )
        self.day_struct = "=bbbffffff"
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

    def read_day_archive(self, path: str):
        files = self.find_files_by_mask(path, self.day_mask)
        day_df = pd.DataFrame()
        for file in files:
            flow_params = self.get_params_from_file_name(file)
            with open(file, "rb") as day_file:
                while True:
                    data = day_file.read(calcsize(self.day_struct))
                    if not data:
                        break
                    day_data = self.DayStruct(*unpack(self.day_struct, data))
                    date_period = datetime(day_data.year + 2000, day_data.month, day_data.day).date()
                    day_data = pd.DataFrame(day_data._asdict(), index=[date_period])
                    day_data['tech'] = flow_params['address']
                    day_data['line'] = flow_params['line']
                    day_data = day_data.drop(columns=['month', 'day', 'year', 'unknown'])
                    day_df = pd.concat([day_df, day_data])
        return day_df


if __name__ == "__main__":
    path = "D:/Projects/HLViewer/HLViewer/develop_data/11Листопад/Zaporizgaz_2024_11_29_8/Zaporizgaz/56ZOPZAP4003301T"
    print(Hostlib().read_day_archive(path))