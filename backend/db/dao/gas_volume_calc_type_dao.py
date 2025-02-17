from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.models import GasVolumeCalcType


class GasVolumeCalcTypeDao(BasicDao):
    def __init__(self, session):
        super().__init__(session=session)
        self.model = GasVolumeCalcType

    async def get_by_type_id(self, type_id: int):
        statement = select(self.model).where(self.model.type_id == type_id)

        result = await self.session.execute(statement)
        result = result.scalars().first()
        if result:
            return result.id
        return None
