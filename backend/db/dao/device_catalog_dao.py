from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, asc

from backend.db.dao.basic_dao import BasicDao
from backend.db.models.device_catalog_model import (
    Manufacturer, ManufacturerCreate, ManufacturerUpdate,
    CorectorType, CorectorTypeCreate, CorectorTypeUpdate,
)


class ManufacturerDao(BasicDao):
    def __init__(self, session: AsyncSession):
        super().__init__(session=session)
        self.model = Manufacturer

    async def get_all(self) -> list[Manufacturer]:
        result = await self.session.execute(
            select(self.model).order_by(asc(self.model.short_name))
        )
        return result.scalars().all()

    async def get_by_short_name(self, short_name: str) -> Manufacturer | None:
        result = await self.session.execute(
            select(self.model).where(self.model.short_name == short_name)
        )
        return result.scalars().first()

    async def create(self, data: ManufacturerCreate) -> Manufacturer:
        return await self.create_item(data)

    async def update(self, item_id: int, data: ManufacturerUpdate) -> Manufacturer | None:
        return await self.update_by_id(item_id, data)

    async def delete(self, item_id: int) -> bool:
        return await self.delete_item(item_id)


class CorectorTypeDao(BasicDao):
    def __init__(self, session: AsyncSession):
        super().__init__(session=session)
        self.model = CorectorType

    async def get_all(self) -> list[CorectorType]:
        result = await self.session.execute(
            select(self.model).order_by(asc(self.model.manufacturer_id), asc(self.model.model_name))
        )
        return result.scalars().all()

    async def get_by_manufacturer(self, manufacturer_id: int) -> list[CorectorType]:
        result = await self.session.execute(
            select(self.model)
            .where(self.model.manufacturer_id == manufacturer_id)
            .order_by(asc(self.model.model_name))
        )
        return result.scalars().all()

    async def get_by_manufacturer_and_model(
        self, manufacturer_id: int, model_name: str
    ) -> CorectorType | None:
        result = await self.session.execute(
            select(self.model).where(
                self.model.manufacturer_id == manufacturer_id,
                self.model.model_name == model_name,
            )
        )
        return result.scalars().first()

    async def create(self, data: CorectorTypeCreate) -> CorectorType:
        return await self.create_item(data)

    async def update(self, item_id: int, data: CorectorTypeUpdate) -> CorectorType | None:
        return await self.update_by_id(item_id, data)

    async def delete(self, item_id: int) -> bool:
        return await self.delete_item(item_id)
