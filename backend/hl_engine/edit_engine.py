from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import EditArchiveCreate
from backend.hl_engine.data_classes.edit_dataclass import EditStruct
from backend.hl_engine.hl_engine import Hostlib


class EditEngine(Hostlib):

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
            mask="S*R*U.*",
            struct=EditStruct,
            create_class=EditArchiveCreate,
            chunk_size=chunk_size,
            lumg_id=lumg_id,
            date_flag=False,
        )
