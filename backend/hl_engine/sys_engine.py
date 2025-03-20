from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.dao.gas_volume_calc_dao import GasVolumeCalcDao
from backend.db.dao.line_dao import LineDao
from backend.db.models import SysArchiveCreate
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
        line_dao = LineDao(session=self.session)
        for file in files:
            flow_params = self.get_params_from_file_name(file)
            gas_volume_calc = await gas_volume_dao.get_or_create(
                address=flow_params["address"], lumg_id=self.lumg_id
            )
            gas_volume_calc_id = gas_volume_calc.id

            gas_volume_line = await line_dao.get_or_create(
                gas_volume_calc_id, flow_params["line"]
            )

            line_id = gas_volume_line.id

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
