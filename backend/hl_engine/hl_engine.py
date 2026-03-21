import os
from datetime import datetime, date
from typing import Type

from sqlalchemy.ext.asyncio import AsyncSession

from sqlmodel import select

from backend.db.dao.gas_volume_calc_dao import GasVolumeCalcDao
from backend.db.dao.line_dao import LineDao
from backend.db.models import HlBaseModel
from backend.db.models.gas_volume_calc_model import GasVolumeCalc
from backend.db.models.line_model import Line as LineModel
from backend.hl_engine.data_classes.base_dataclass import BaseDataclass
from utils.files_utils import find_files_by_mask, read_archive_file
from utils.logger import logger_setup


class Hostlib:
    def __init__(
        self,
        session: AsyncSession,
        mask: str,
        struct: Type[BaseDataclass],
        create_class: Type[HlBaseModel],
        path: str = "./",
        chunk_size: int = 900,
        lumg_id: int = 1,
        date_flag: bool = True,
    ) -> None:
        self.logger = logger_setup("backend")
        self.path = path
        self.chunk_size = chunk_size
        self.lumg_id = lumg_id
        self.session = session
        self.mask = mask
        self.struct = struct
        self.create_class = create_class
        self.date_flag = date_flag

    async def read(self):
        files = find_files_by_mask(self.path, self.mask)
        archive_dict_list = []
        gas_volume_dao = GasVolumeCalcDao(session=self.session)
        line_dao = LineDao(session=self.session)
        _calc_cache: dict[int, int] = {}          # address → gas_volume_calc_id
        _line_cache: dict[tuple, int] = {}         # (calc_id, line_num) → line_id

        # Pre-load all known GVC and Line records for this lumg in 2 queries
        gvc_result = await self.session.execute(
            select(GasVolumeCalc).where(GasVolumeCalc.lumg_id == self.lumg_id)
        )
        for gvc in gvc_result.scalars().all():
            _calc_cache[gvc.address] = gvc.id

        if _calc_cache:
            line_result = await self.session.execute(
                select(LineModel).where(LineModel.gas_volume_calc_id.in_(_calc_cache.values()))
            )
            for ln in line_result.scalars().all():
                _line_cache[(ln.gas_volume_calc_id, ln.line)] = ln.id

        for file in files:
            flow_params = self.get_params_from_file_name(file)
            address = flow_params["address"]
            line_num = flow_params["line"]

            if address not in _calc_cache:
                gas_volume_calc = await gas_volume_dao.get_or_create(
                    address=address, lumg_id=self.lumg_id
                )
                _calc_cache[address] = gas_volume_calc.id
            gas_volume_calc_id = _calc_cache[address]

            key = (gas_volume_calc_id, line_num)
            if key not in _line_cache:
                gas_volume_line = await line_dao.get_or_create(gas_volume_calc_id, line_num)
                _line_cache[key] = gas_volume_line.id
            line_id = _line_cache[key]

            read_archive_gen = read_archive_file(file, self.struct)
            while True:
                try:
                    file_dict = next(read_archive_gen)
                    if self.date_flag:
                        datetime_period = date(
                            file_dict["year"] + 2000,
                            file_dict["month"],
                            file_dict["day"],
                        )
                    else:
                        datetime_period = datetime(
                            file_dict["year"] + 2000,
                            file_dict["month"],
                            file_dict["day"],
                            file_dict["hour"],
                            file_dict["minutes"],
                            file_dict.get("seconds", 0),
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

    @staticmethod
    def get_params_from_file_name(path_to_file: str) -> dict:
        filename = os.path.basename(path_to_file)
        address = int(filename[1:4])
        line = int(filename[5:6])
        return {"address": address, "line": line}


if __name__ == "__main__":
    path_dir = "D:/Projects/HLViewer/HLViewer/develop_data/11Листопад/Zaporizgaz_2024_11_29_8/Zaporizgaz/56ZOPZAP4003301T"
