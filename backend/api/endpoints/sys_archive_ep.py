from datetime import datetime

from fastapi import Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.endpoints.auth_ep import get_allowed_line_ids
from backend.api.endpoints.base_archive_ep import BaseArchiveRouter
from backend.db.dao.sys_archive_dao import SysArchiveDao
from backend.db.engine import get_session
from backend.db.models import SysArchiveEndpointList
from backend.db.models.sys_archive_model import SysGroupedItem


class SysArchiveRouter(BaseArchiveRouter):
    def __init__(self):
        super().__init__(
            path="/sys/",
            archive_list_class=SysArchiveEndpointList,
            tags=["sys"],
            archive_dao=SysArchiveDao,
        )
        self.router.add_api_route(
            path="/sys/grouped/",
            endpoint=self._get_grouped,
            response_model=list[SysGroupedItem],
            tags=["sys"],
            methods=["GET"],
        )
        self.router.add_api_route(
            path="/sys/events/",
            endpoint=self._get_events,
            tags=["sys"],
            methods=["GET"],
        )
        self.router.add_api_route(
            path="/sys/paged/",
            endpoint=self._get_paged,
            tags=["sys"],
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
        session: AsyncSession = Depends(get_session),
    ):
        self._check_dates(from_date, to_date)
        line_id = self._scope_line_ids(line_id, allowed_line_ids)
        if self._scope_is_empty(line_id):
            return {"total": 0, "items": []}
        dao = SysArchiveDao(session=session)
        return await dao.get_range_paged(
            from_date, to_date, line_id,
            skip=skip, limit=limit, order_by=order_by, order_dir=order_dir,
        )

    async def _get_events(
        self,
        from_date: datetime = Query(None),
        to_date: datetime = Query(None),
        line_id: list[int] = Query(None),
        allowed_line_ids: list[int] | None = Depends(get_allowed_line_ids),
        session: AsyncSession = Depends(get_session),
    ):
        """Every event of a period in the compact shape the accidents report reads.

        Returns the JSON straight from the DAO: with hundreds of thousands of
        rows, a response model would spend longer validating them than the
        database spent producing them."""
        self._check_dates(from_date, to_date)
        line_id = self._scope_line_ids(line_id, allowed_line_ids)
        if self._scope_is_empty(line_id):
            return Response(content='{"names":[],"rows":[]}', media_type="application/json")
        dao = SysArchiveDao(session=session)
        body = await dao.get_events_json(from_date, to_date, line_id)
        return Response(content=body, media_type="application/json")

    async def _get_grouped(
        self,
        from_date: datetime = Query(None),
        to_date: datetime = Query(None),
        line_id: list[int] = Query(None),
        allowed_line_ids: list[int] | None = Depends(get_allowed_line_ids),
        session: AsyncSession = Depends(get_session),
    ):
        self._check_dates(from_date, to_date)
        line_id = self._scope_line_ids(line_id, allowed_line_ids)
        if self._scope_is_empty(line_id):
            return []
        dao = SysArchiveDao(session=session)
        return await dao.get_grouped(from_date, to_date, line_id)


sys_router = SysArchiveRouter().router
