from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.dao.basic_dao import BasicDao
from backend.db.models import DailyArchive


class DailyArchiveDao(BasicDao):
    def __init__(self, session: AsyncSession):
        super().__init__(session=session)
        self.model = DailyArchive


if __name__ == "__main__":
    archives = DailyArchiveDao().get_range(gas_volume_calc_id=[1, 2])
    pass
