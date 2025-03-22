from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.dao.basic_dao import BasicDao
from backend.db.models import Param


class ParamDao(BasicDao):
    def __init__(self, session: AsyncSession):
        super().__init__(session=session)
        self.model = Param
