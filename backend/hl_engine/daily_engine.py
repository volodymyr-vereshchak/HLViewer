from collections import namedtuple
from datetime import date

from backend.db.dao.gas_volume_calc_dao import GasVolumeCalcDao
from backend.db.dao.line_dao import LineDao
from backend.db.models import DailyArchiveCreate
from backend.hl_engine.data_classes.day_dataclass import DayStruct
from backend.hl_engine.hl_engine import Hostlib
from utils.files_utils import find_files_by_mask, read_archive_file


class DailyEngine(Hostlib):

    def __init__(self, path: str = "./", chunk_size: int = 900) -> None:
        super().__init__(path, chunk_size)
        self.day_mask = "S*R*D.*"
        self.day_struct = DayStruct
        self.create_class = DailyArchiveCreate

    def read(self):
        files = find_files_by_mask(self.path, self.day_mask)
        archive_dict_list = []
        gas_volume_dao = GasVolumeCalcDao()
        line_dao = LineDao()
        for file in files:
            flow_params = self.get_params_from_file_name(file)
            gas_volume_calc = gas_volume_dao.get_flow_calc_by_address_or_create(
                flow_params["address"]
            )
            gas_volume_calc_id = gas_volume_calc.id

            gas_volume_line = line_dao.get_line_by_gas_id_and_line_or_create(
                gas_volume_calc_id, flow_params["line"]
            )

            line_id = gas_volume_line.id

            read_archive_gen = read_archive_file(file, self.day_struct)

            while True:
                try:
                    file_dict = next(read_archive_gen)

                    datetime_period = date(
                        file_dict["year"] + 2000, file_dict["month"], file_dict["day"]
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
