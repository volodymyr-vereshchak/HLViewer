from backend.db.dao.basic_dao import BasicDao
from backend.db.models import SysArchive


class SysArchiveDao(BasicDao):
    def __init__(self):
        super().__init__()
        self.model = SysArchive


if __name__ == "__main__":
    result = SysArchiveDao().get_data_counts_by_hour(line_id=[5])
    print(result)
