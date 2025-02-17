from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.models import Lumg, LumgCreate, LumgUpdate


class LumgDao(BasicDao):
    def __init__(self, session: AsyncSession):
        super().__init__(session=session)
        self.model = Lumg

    async def get(self, name: str):
        statement = select(self.model).where(self.model.name == name)
        result = await self.session.execute(statement)
        return result.scalars().first()

    async def update_if_exist(self, name: str):
        result = await self.get(name)
        if result:
            result = await self.update_by_id(result.id, LumgUpdate(name=name))
        return result

    async def get_or_create(self, name: str):
        result = await self.get(name)
        if not result:
            result = await self.create_item(LumgCreate(name=name))

        return result


if __name__ == "__main__":
    # from backend.db.models.lumg_model import LumgUpdate

    lumg_db = LumgUpdate(name="LVUMG")
    lumg = LumgDao().update_by_id(1, LumgUpdate(name="LVUMG"))
    pass
