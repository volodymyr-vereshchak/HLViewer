from backend.db.dao.basic_dao import BasicDao
from backend.db.models import HourlyArchive


class HourlyArchiveDao(BasicDao):
    def __init__(self):
        super().__init__()
        self.model = HourlyArchive
