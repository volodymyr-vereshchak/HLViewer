from sqlalchemy.ext.asyncio import AsyncSession
from backend.db.models import ParamCreate
from backend.hl_engine.data_classes.param_dataclass import ParamStruct
from backend.hl_engine.hl_engine import Hostlib


class ParamEngine(Hostlib):

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
            mask="S*R*S.*",
            struct=ParamStruct,
            create_class=ParamCreate,
            chunk_size=chunk_size,
            lumg_id=lumg_id,
            date_flag=False,
        )
