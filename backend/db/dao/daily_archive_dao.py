from backend.db.dao.basic_dao import BasicDao
from backend.db.models import DailyArchive


class DailyArchiveDao(BasicDao):
    def __init__(self):
        super().__init__()
        self.model = DailyArchive


if __name__ == "__main__":
    archives = DailyArchiveDao().get_all()
    pass