from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import HourlyArchiveCreate
from backend.hl_engine.data_classes.hour_dataclass import HourStruct
from backend.hl_engine.hl_engine import Hostlib


class HourlyEngine(Hostlib):

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
            mask="S*R*R.*",
            struct=HourStruct,
            create_class=HourlyArchiveCreate,
            chunk_size=chunk_size,
            lumg_id=lumg_id,
            date_flag=False,
        )
