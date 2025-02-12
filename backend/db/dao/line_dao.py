from time import sleep

from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.models import Line, LineCreate, LineUpdate


class LineDao(BasicDao):
    def __init__(self):
        super().__init__()
        self.model = Line

    def get(self, gas_volume_calc_id: int, line: int):
        session_db = self.get_session()
        statement = select(self.model).where(
            (self.model.gas_volume_calc_id == gas_volume_calc_id)
            & (self.model.line == line)
        )
        with session_db as session:
            result = session.exec(statement).first()
        return result

    def update_if_exists(
        self,
        gas_volume_calc_id: int,
        line: int,
        meter: bool = False,
        name: str = None,
    ):
        result = self.get(gas_volume_calc_id=gas_volume_calc_id, line=line)
        if result:
            line = LineUpdate(
                line=line,
                name=name,
                gas_volume_calc_id=gas_volume_calc_id,
                meter=meter,
            )
            result = self.update_by_id(result.id, line)
            self.logger.debug(
                f"Gas volume id: {gas_volume_calc_id}. Line with this number: {line} was updated!"
            )
        return result

    def get_or_create(
        self,
        gas_volume_calc_id: int,
        line: int,
        meter: bool = False,
        name: str = None,
    ):
        result = self.get(gas_volume_calc_id=gas_volume_calc_id, line=line)

        if not result:
            name = name if name else f"l{line}"
            line_inst = LineCreate(
                line=line,
                name=name,
                gas_volume_calc_id=gas_volume_calc_id,
                meter=meter,
            )
            try:
                result = self.create_line(line_inst)
                self.logger.debug(f"No line with this number: {line} Created new!")
            except DatabaseIntegrityError:
                sleep(1)
                self.logger.debug(f"Line with this number: {line} is created!")
                result = self.get(gas_volume_calc_id=gas_volume_calc_id, line=line)

        return result

    def get_line_by_lumg_id(self, lumg_id: int = None):
        statement = select(self.model).join(self.model.gas_volume_calc)
        if lumg_id:
            statement = statement.where(self.model.gas_volume_calc.has(lumg_id=lumg_id))

        with self.get_session() as session:
            return session.exec(statement).all()

    def create_line(self, line: LineCreate):
        line_db = self.create_item(line)
        return line_db
