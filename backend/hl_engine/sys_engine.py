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
        super().__init__(
            session=session,
            path=path,
            mask="S*R*A.*",
            struct=SysStruct,
            create_class=SysArchiveCreate,
            chunk_size=chunk_size,
            lumg_id=lumg_id,
            date_flag=False,
        )
