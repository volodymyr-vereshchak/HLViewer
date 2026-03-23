from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.models import GasVolumeCalcType
from backend.db.models.gas_volume_calc_type_model import GasVolumeCalcTypeCreate


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

    async def get_or_create_by_type_id(self, type_id: int) -> int:
        """Return DB id for the given type_id, auto-creating with numeric name if missing."""
        existing_id = await self.get_by_type_id(type_id)
        if existing_id is not None:
            return existing_id
        try:
            new = await self.create_item(GasVolumeCalcTypeCreate(
                type_id=type_id,
                type_name=str(type_id),
            ))
            self.logger.info(f"Auto-created unknown calc type: type_id={type_id}")
            return new.id
        except DatabaseIntegrityError:
            # Race condition — another worker created it first
            return await self.get_by_type_id(type_id)
