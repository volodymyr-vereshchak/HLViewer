from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, asc

from backend.db.dao.basic_dao import BasicDao
from backend.db.models.enterprise_model import Enterprise, EnterpriseCreate, EnterpriseUpdate
from backend.db.models.device_catalog_model import CorectorType, Manufacturer


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

    async def get_by_device(self, ser_num: int, mf_dev: int, type_dev: int, ch_num: int) -> Enterprise | None:
        # Match against the EFFECTIVE codes: catalog values when the enterprise is
        # linked to a corrector type, otherwise the legacy mf_dev/type_dev columns.
        # Callers pass the effective codes (that's what our API surfaces).
        eff_mf = func.coalesce(Manufacturer.mf_dev, self.model.mf_dev)
        eff_type = func.coalesce(CorectorType.type_dev, self.model.type_dev)
        stmt = (
            select(self.model)
            .outerjoin(CorectorType, self.model.corector_type_id == CorectorType.id)
            .outerjoin(Manufacturer, CorectorType.manufacturer_id == Manufacturer.id)
            .where(self.model.ser_num == ser_num)
            .where(self.model.ch_num == ch_num)
            .where(eff_mf == mf_dev)
            .where(eff_type == type_dev)
            .where(self.model.active == True)  # noqa: E712
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()
