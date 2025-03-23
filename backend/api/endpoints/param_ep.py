from datetime import datetime

from fastapi import Query

from backend.api.endpoints.base_archive_ep import BaseArchiveRouter
from backend.db.dao.param_dao import ParamDao
from backend.db.engine import async_session_factory
from backend.db.models import ParamList


class ParamRouter(BaseArchiveRouter):
    def __init__(self):
        super().__init__(
            path="/param/",
            archive_list_class=ParamList,
            tags=["param"],
            archive_dao=ParamDao,
        )

    async def get_archive(
        self,
        from_date: datetime = Query(None),
        to_date: datetime = Query(None),
        line_id: list[int] = Query(None),
    ):
        async with async_session_factory() as session:
            archive_dao = self.archive_dao(session=session)
            archives = await archive_dao.get_last_for_to_date(to_date, line_id)
            return archives


param_router = ParamRouter().router
