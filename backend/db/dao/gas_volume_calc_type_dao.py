from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.models import GasVolumeCalcType


class GasVolumeCalcTypeDao(BasicDao):
    def __init__(self):
        super().__init__()
        self.model = GasVolumeCalcType

    def get_by_type_id(self, type_id: int):
        session_db = self.get_session()
        statement = select(self.model).where(self.model.type_id == type_id)

        with session_db as session:
            result = session.exec(statement).first()
            if result:
                return result.id
            return None
