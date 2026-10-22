from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import DailyArchiveCreate
from backend.hl_engine.data_classes.day_dataclass import DayStruct
from backend.hl_engine.hl_engine import Hostlib


class DailyEngine(Hostlib):

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
            mask="S*R*D.*",
            struct=DayStruct,
            create_class=DailyArchiveCreate,
            chunk_size=chunk_size,
            lumg_id=lumg_id,
            merge_duplicates=True,
            date_flag=True,
        )
