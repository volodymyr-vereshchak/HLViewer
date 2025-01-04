from backend.db.dao.basic_dao import BasicDao
from backend.db.models import GasVolumeCalcType


class GasVolumeCalcTypeDao(BasicDao):
    def __init__(self):
        super().__init__()
        self.model = GasVolumeCalcType
