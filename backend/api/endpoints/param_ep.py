from datetime import datetime

from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.endpoints.auth_ep import get_allowed_line_ids
from backend.api.endpoints.base_archive_ep import BaseArchiveRouter
from backend.db.dao.param_dao import ParamDao
from backend.db.engine import get_session
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
        session: AsyncSession = Depends(get_session),
    ):
        """Two shapes, chosen by what the caller asks for.

        With BOTH dates: every record in the range, like any other archive.
        With one or neither: the latest record per line at or before ``to_date``
        — the configuration snapshot the overview reads ``min_dp``/``max_dp``
        from. That snapshot is why the inherited range query was replaced here
        back in 2025; callers that want the range now say so explicitly instead
        of the two uses fighting over one default.
        """
        line_id = self._scope_line_ids(line_id, allowed_line_ids)
        if self._scope_is_empty(line_id):
            return []
        archive_dao = self.archive_dao(session=session)
        if from_date and to_date:
            return await archive_dao.get_range(from_date, to_date, line_id)
        if line_id and len(line_id) > 1:
            archives = await archive_dao.get_last_per_line_ids(to_date, line_id)
            return archives
        else:
            archive = await archive_dao.get_last_for_to_date(to_date, line_id)
            return [archive] if archive else []


param_router = ParamRouter().router
