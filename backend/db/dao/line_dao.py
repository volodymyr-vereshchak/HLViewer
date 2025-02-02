from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.models import Line, LineCreate, LineUpdate


class LineDao(BasicDao):
    def __init__(self):
        super().__init__()
        self.model = Line

    def get_line_by_gas_id_and_line_or_create(
        self, gas_volume_calc_id: int, line: int, meter: bool = False, name: str = None
    ):
        session_db = self.get_session()
        statement = select(self.model).where(
            (self.model.gas_volume_calc_id == gas_volume_calc_id)
            & (self.model.line == line)
        )
        with session_db as session:
            result = session.exec(statement).first()

        if result:
            line = LineUpdate(
                line=line,
                name=name,
                gas_volume_calc_id=gas_volume_calc_id,
                meter=meter,
            )
            result = self.update_by_id(result.id, line)

        else:
            name = name if name else f"l{line}"
            line = LineCreate(
                line=line,
                name=name,
                gas_volume_calc_id=gas_volume_calc_id,
                meter=meter,
            )
            result = self.create_line(line)
            self.logger.debug(f"No line with this number: {line} Created new!")

        return result

    def get_line_by_lumg_id(self, lumg_id: int = None):
        statement = select(self.model)
        if lumg_id:
            statement = statement.where(self.model.gas_volume_calc.lumg_id == lumg_id)

        with self.get_session() as session:
            return session.exec(statement).all()

    def create_line(self, line: LineCreate):
        line_db = self.create_item(line)
        return line_db
