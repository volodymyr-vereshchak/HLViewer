from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.models import GasVolumeCalc


class GasVolumeCalcDao(BasicDao):
    def __init__(self):
        super().__init__()
        self.model = GasVolumeCalc

    def get_id_by_address(self, address: int):
        statement = select(self.model).where(self.model.address == address)
        with self.session as session:
            result = session.exec(statement).first()
        if result:
            return result.id
        return None
