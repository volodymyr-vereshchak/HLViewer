from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.models import GasVolumeCalc, GasVolumeCalcCreate
from backend.db.models.gas_volume_calc_model import GasVolumeCalcUpdate


class GasVolumeCalcDao(BasicDao):
    def __init__(self, session: AsyncSession):
        super().__init__(session=session)
        self.model = GasVolumeCalc

    async def get(self, address: int, lumg_id: int):
        statement = select(self.model).where(
            (self.model.address == address) & (self.model.lumg_id == lumg_id)
        )
        result = await self.session.execute(statement)
        return result.scalars().first()

    async def update_if_exists(
        self,
        address: int,
        lumg_id: int,
        type_id: int | None = None,
        c_time: int = 7,
        name: str = None,
    ):
        result = await self.get(address=address, lumg_id=lumg_id)
        if result:
            gvc = GasVolumeCalcUpdate(
                address=address,
                name=name,
                c_time=c_time,
                lumg_id=lumg_id,
                type_id=type_id,
            )
            result = await self.update_by_id(result.id, gvc)
            self.logger.debug(
                f"Gas volume calc with this address: {address} was updated {type_id}!"
            )
        return result

    async def get_or_create(
        self,
        address: int,
        lumg_id: int,
        type_id: int | None = None,
        c_time: int = 7,
        name: str = None,
    ):
        result = await self.get(address=address, lumg_id=lumg_id)

        if not result:
            if not name:
                name = f"a{address}"
            gvc = GasVolumeCalcCreate(
                address=address,
                name=name,
                c_time=c_time,
                lumg_id=lumg_id,
                type_id=type_id,
            )
            try:
                result = await self.create_flow_calc(gvc)
                self.logger.debug(
                    f"No gas volume calc with this address: {address} Created new!"
                )
            except DatabaseIntegrityError:
                self.logger.debug(
                    f"Gas volume calc with this address: {address} is created!"
                )
                result = await self.get(address=address, lumg_id=lumg_id)

        return result

    async def create_flow_calc(self, gvc: GasVolumeCalcCreate):
        gas_volume_calc = await self.create_item(gvc)
        return gas_volume_calc

    async def get_flow_by_lumg_id(self, lumg_id: int = None):
        statement = select(self.model)
        if lumg_id:
            statement = statement.where(self.model.lumg_id == lumg_id)

        result = await self.session.execute(statement)
        return result.scalars().all()
