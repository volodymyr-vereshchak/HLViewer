from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.models import Line, LineCreate, LineUpdate


class LineDao(BasicDao):
    def __init__(self, session: AsyncSession):
        super().__init__(session=session)
        self.model = Line

    async def get(self, gas_volume_calc_id: int, line: int):
        statement = select(self.model).where(
            (self.model.gas_volume_calc_id == gas_volume_calc_id)
            & (self.model.line == line)
        )
        result = await self.session.execute(statement)
        return result.scalars().first()

    async def update_if_exists(
        self,
        gas_volume_calc_id: int,
        line: int,
        meter: bool = False,
        name: str = None,
    ):
        result = await self.get(gas_volume_calc_id=gas_volume_calc_id, line=line)
        if result:
            line = LineUpdate(
                line=line,
                name=name,
                gas_volume_calc_id=gas_volume_calc_id,
                meter=meter,
            )
            result = await self.update_by_id(result.id, line)
            self.logger.debug(
                f"Gas volume id: {gas_volume_calc_id}. Line with this number: {line} was updated!"
            )
        return result

    async def get_or_create(
        self,
        gas_volume_calc_id: int,
        line: int,
        meter: bool = False,
        name: str = None,
    ):
        result = await self.get(gas_volume_calc_id=gas_volume_calc_id, line=line)

        if not result:
            name = name if name else f"l{line}"
            line_inst = LineCreate(
                line=line,
                name=name,
                gas_volume_calc_id=gas_volume_calc_id,
                meter=meter,
            )
            try:
                result = await self.create_line(line_inst)
                self.logger.debug(f"No line with this number: {line} Created new!")
            except DatabaseIntegrityError:
                self.logger.debug(f"Line with this number: {line} is created!")
                result = await self.get(
                    gas_volume_calc_id=gas_volume_calc_id, line=line
                )

        return result

    async def get_line_by_lumg_id(self, lumg_id: int = None):
        statement = select(self.model).join(self.model.gas_volume_calc)
        if lumg_id:
            statement = statement.where(self.model.gas_volume_calc.has(lumg_id=lumg_id))

        result = await self.session.execute(statement)
        return result.scalars().all()

    async def create_line(self, line: LineCreate):
        line_db = await self.create_item(line)
        return line_db

    async def get_line_name_by_id(self, id_line: int):
        statement = select(self.model).where((self.model.id == id_line))
        result = await self.session.execute(statement)
        return result.scalars().first()
