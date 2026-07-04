from datetime import datetime

from fastapi import Depends, Query

from backend.api.endpoints.auth_ep import get_allowed_line_ids
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
        allowed_line_ids: list[int] | None = Depends(get_allowed_line_ids),
    ):
        line_id = self._scope_line_ids(line_id, allowed_line_ids)
        if self._scope_is_empty(line_id):
            return []
        async with async_session_factory() as session:
            archive_dao = self.archive_dao(session=session)
            if line_id and len(line_id) > 1:
                archives = await archive_dao.get_last_per_line_ids(to_date, line_id)
                return archives
            else:
                archive = await archive_dao.get_last_for_to_date(to_date, line_id)
                return [archive] if archive else []


param_router = ParamRouter().router
