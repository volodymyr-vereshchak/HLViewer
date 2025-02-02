from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.models import Line, LineCreate


class LineDao(BasicDao):
    def __init__(self):
        super().__init__()
        self.model = Line

    def get_line_by_gas_id_and_line_or_create(self, gas_volume_calc_id: int, line: int):
        session_db = self.get_session()
        statement = select(self.model).where(
            (self.model.gas_volume_calc_id == gas_volume_calc_id)
            & (self.model.line == line)
        )
        with session_db as session:
            result = session.exec(statement).first()

        if not result:
            result = self.create_default_line(gas_volume_calc_id, line)
            self.logger.debug(
                f"No gas volume calc with this address: {address} Created new!"
            )

        return result

    def create_default_line(self, gas_volume_calc_id: int, line: int):
        line = LineCreate(
            line=line,
            name=f"l{line}",
            gas_volume_calc_id=gas_volume_calc_id,
            meter=False,
        )
        line_db = self.create_item(line)
        return line_db
