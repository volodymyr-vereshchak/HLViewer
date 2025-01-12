from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.models import GasVolumeCalc


class GasVolumeCalcDao(BasicDao):
    def __init__(self):
        super().__init__()
        self.model = GasVolumeCalc

    def get_flow_calc_by_address_and_line(self, address: int, line: int):
        session_db = self.get_session()
        statement = select(self.model).where(
            (self.model.address == address) & (self.model.line == line)
        )
        with session_db as session:
            return session.exec(statement).first()
