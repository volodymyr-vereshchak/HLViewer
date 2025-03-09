from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.models import SysArchive, SysType


class SysArchiveDao(BasicDao):
    def __init__(self, session: AsyncSession):
        super().__init__(session=session)
        self.model = SysArchive

    async def get_range(
        self,
        from_date: datetime = None,
        to_date: datetime = None,
        line_id: list[int] = None,
    ):
        statement = select(self.model, SysType).join(self.model.sys_type)
        if from_date:
            statement = statement.where(self.model.period >= from_date)
        if to_date:
            statement = statement.where(self.model.period <= to_date)
        if line_id:
            statement = statement.where(self.model.line_id.in_(line_id))

        query = await self.session.execute(statement)
        result = []
        for sys_archive, sys_type in query.all():
            row = sys_archive.model_dump()
            row["sys_name"] = sys_type.sys_name
            result.append(row)
        return result


if __name__ == "__main__":
    # result = SysArchiveDao().get_data_counts_by_hour(line_id=[5])
    # print(result)
    pass
