import os
import glob
import pandas as pd
from datetime import datetime
from struct import calcsize, unpack
from collections import namedtuple

from backend.db.engine import create_db_engine
from sqlmodel import Session

from backend.db.models import DailyArchive, HourlyArchive


class Hostlib:
    def __init__(self, at: int = 0, path: str = "./") -> None:
        self.path = path
        self.db_engine = create_db_engine()
        self.session = Session(self.db_engine)
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

    def read_daily_archive(self):
        files = self.find_files_by_mask(self.path, self.day_mask)
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
                    day_data = pd.DataFrame([day_data._asdict()])
                    day_data["period"] = date_period
                    day_data["tech"] = flow_params["address"]
                    day_data["line"] = flow_params["line"]
                    day_data = day_data.drop(columns=['month', 'day', 'year', 'unknown'])
                    day_df = pd.concat([day_df, day_data])
        self.session.bulk_insert_mappings(DailyArchive, day_df.to_dict(orient="records"))
        self.session.commit()
        return day_df

    def read_hourly_archive(self):
        files = self.find_files_by_mask(self.path, self.hour_mask)
        hour_df = pd.DataFrame()
        for file in files:
            flow_params = self.get_params_from_file_name(file)
            with open(file, "rb") as hour_file:
                while True:
                    data = hour_file.read(calcsize(self.hour_struct))
                    if not data:
                        break
                    hour_data = self.HourStruct(*unpack(self.hour_struct, data))
                    datetime_period = datetime(hour_data.year + 2000, hour_data.month, hour_data.day, hour_data.hour, hour_data.minutes)
                    hour_data = pd.DataFrame(hour_data._asdict())
                    hour_data["period"] = datetime_period
                    hour_data['tech'] = flow_params['address']
                    hour_data['line'] = flow_params['line']
                    hour_data = hour_data.drop(columns=["month", "day", "year", "hour", "minutes", "unknown"])
                    hour_df = pd.concat([hour_df, hour_data])
        self.session.bulk_insert_mappings(HourlyArchive, hour_df.to_dict(orient="records"))
        self.session.commit()
        return hour_df


if __name__ == "__main__":
    path_dir = "D:/Projects/HLViewer/HLViewer/develop_data/11Листопад/Zaporizgaz_2024_11_29_8/Zaporizgaz/56ZOPZAP4003301T"
    print(Hostlib(path=path_dir).read_daily_archive())
