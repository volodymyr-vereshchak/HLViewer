from datetime import datetime

from fastapi import Depends, Query

from backend.api.endpoints.auth_ep import get_allowed_line_ids
from backend.api.endpoints.base_archive_ep import BaseArchiveRouter
from backend.db.dao.edit_archive_dao import EditArchiveDao
from backend.db.engine import async_session_factory
from backend.db.models import EditArchiveEndpointList


class EditArchiveRouter(BaseArchiveRouter):
    def __init__(self):
        super().__init__(
            path="/edit/",
            archive_list_class=EditArchiveEndpointList,
            tags=["edit"],
            archive_dao=EditArchiveDao,
            max_days=30,
        )
        self.router.add_api_route(
            path="/edit/paged/",
            endpoint=self._get_paged,
            tags=["edit"],
            methods=["GET"],
        )

    async def _get_paged(
        self,
        from_date: datetime = Query(None),
        to_date: datetime = Query(None),
        line_id: list[int] = Query(None),
        skip: int = Query(0),
        limit: int = Query(50),
        order_by: str = Query("period"),
        order_dir: str = Query("asc"),
        allowed_line_ids: list[int] | None = Depends(get_allowed_line_ids),
    ):
        self._check_dates(from_date, to_date)
        line_id = self._scope_line_ids(line_id, allowed_line_ids)
        if self._scope_is_empty(line_id):
            return {"total": 0, "items": []}
        async with async_session_factory() as session:
            dao = EditArchiveDao(session=session)
            return await dao.get_range_paged(
                from_date, to_date, line_id,
                skip=skip, limit=limit, order_by=order_by, order_dir=order_dir,
            )


edit_router = EditArchiveRouter().router
