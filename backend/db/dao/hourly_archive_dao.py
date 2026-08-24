from datetime import datetime

from sqlalchemy import extract
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.models import HourlyArchive


class HourlyArchiveDao(BasicDao):
    def __init__(self, session: AsyncSession):
        super().__init__(session=session)
        self.model = HourlyArchive

    async def load_volumes(
        self,
        from_date: datetime,
        to_date: datetime,
        line_id: list[int],
        hours: list[int] | None = None,
    ) -> list[tuple[int, datetime, float]]:
        """(line_id, period, volume) triples — the columns, not the row.

        `get_range` builds an ORM object per row, which for a month over a
        whole branch is a quarter of a million throwaway objects. The night
        report reads three of the eight columns and needs no identity, so it
        reads columns instead: same rows, a fifth of the time.

        `hours` narrows to those wall-clock hours (the report looks at nine of
        twenty-four), evaluated in the database so the rest is never fetched.
        """
        statement = select(
            self.model.line_id, self.model.period, self.model.volume
        ).where(
            self.model.period >= from_date,
            self.model.period <= to_date,
            self.model.line_id.in_(line_id),
        )
        if hours:
            statement = statement.where(
                extract("hour", self.model.period).in_(hours)
            )
        return (await self.session.execute(statement)).all()
