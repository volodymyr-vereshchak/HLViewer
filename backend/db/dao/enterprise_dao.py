from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, asc

from backend.db.dao.basic_dao import BasicDao
from backend.db.models.enterprise_model import Enterprise, EnterpriseCreate, EnterpriseUpdate


class EnterpriseDao(BasicDao):
    def __init__(self, session: AsyncSession):
        super().__init__(session=session)
        self.model = Enterprise

    async def get_all(self) -> list[Enterprise]:
        stmt = (
            select(self.model)
            .order_by(asc(self.model.line_id.is_(None)), asc(self.model.line_id), asc(self.model.enterprise_name))
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_by_branch_ids(self, branch_ids: list[int]) -> list[Enterprise]:
        stmt = (
            select(self.model)
            .where(self.model.branch_id.in_(branch_ids))
            .order_by(asc(self.model.line_id.is_(None)), asc(self.model.line_id), asc(self.model.enterprise_name))
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_by_id(self, enterprise_id: int) -> Enterprise | None:
        return await self.session.get(self.model, enterprise_id)

    async def create(self, data: EnterpriseCreate) -> Enterprise:
        return await self.create_item(data)

    async def update(self, enterprise_id: int, data: EnterpriseUpdate) -> Enterprise | None:
        return await self.update_by_id(enterprise_id, data)

    async def delete(self, enterprise_id: int) -> bool:
        return await self.delete_item(enterprise_id)

    async def get_active_for_lines(self, line_ids: list[int]) -> list[Enterprise]:
        stmt = (
            select(self.model)
            .where(self.model.active == True)  # noqa: E712
            .where(self.model.line_id.in_(line_ids))
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_by_ser_ch(self, ser_num: int, ch_num: int) -> Enterprise | None:
        stmt = (
            select(self.model)
            .where(self.model.ser_num == ser_num)
            .where(self.model.ch_num == ch_num)
            .where(self.model.active == True)  # noqa: E712
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()
