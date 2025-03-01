from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.models import EditArchive, EditType


class EditArchiveDao(BasicDao):
    def __init__(self, session: AsyncSession):
        super().__init__(session=session)
        self.model = EditArchive

    async def get_range(
        self,
        from_date: datetime = None,
        to_date: datetime = None,
        line_id: list[int] = None,
    ):
        statement = select(self.model, EditType).join(self.model.edit)
        if from_date:
            statement = statement.where(self.model.period >= from_date)
        if to_date:
            statement = statement.where(self.model.period <= to_date)
        if line_id:
            statement = statement.where(self.model.line_id.in_(line_id))

        query = await self.session.execute(statement)
        result = []
        for edit_archive, edit_type in query.all():
            row = edit_archive.model_dump()
            row["edit_name"] = edit_type.edit_name
            result.append(row)
        return result


if __name__ == "__main__":
    import asyncio
    from backend.db.engine import async_session_factory

    async def get_range():
        async with async_session_factory() as session:
            result = await EditArchiveDao(session).get_range()
            print(result)

    asyncio.run(get_range())
