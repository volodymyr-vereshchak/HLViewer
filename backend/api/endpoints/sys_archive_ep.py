from datetime import datetime

from fastapi import Depends, HTTPException, Query

from backend.api.endpoints.auth_ep import get_allowed_line_ids
from backend.api.endpoints.base_archive_ep import BaseArchiveRouter
from backend.db.dao.sys_archive_dao import SysArchiveDao
from backend.db.engine import async_session_factory
from backend.db.models import SysArchiveEndpointList
from backend.db.models.sys_archive_model import SysGroupedItem


class SysArchiveRouter(BaseArchiveRouter):
    def __init__(self):
        super().__init__(
            path="/sys/",
            archive_list_class=SysArchiveEndpointList,
            tags=["sys"],
            archive_dao=SysArchiveDao,
            max_days=30,
        )
        self.router.add_api_route(
            path="/sys/grouped/",
            endpoint=self._get_grouped,
            response_model=list[SysGroupedItem],
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
    ):
        self._check_dates(from_date, to_date)
        line_id = self._scope_line_ids(line_id, allowed_line_ids)
        async with async_session_factory() as session:
            dao = SysArchiveDao(session=session)
            return await dao.get_range_paged(
                from_date, to_date, line_id,
                skip=skip, limit=limit, order_by=order_by, order_dir=order_dir,
            )

    async def _get_grouped(
        self,
        from_date: datetime = Query(None),
        to_date: datetime = Query(None),
        line_id: list[int] = Query(None),
        allowed_line_ids: list[int] | None = Depends(get_allowed_line_ids),
    ):
        if not from_date or not to_date:
            raise HTTPException(status_code=400, detail="from_date and to_date are required")
        if (to_date - from_date).days > self.max_days:
            raise HTTPException(
                status_code=400, detail=f"Date range exceeds {self.max_days} days"
            )
        if allowed_line_ids is not None:
            allowed_set = set(allowed_line_ids)
            line_id = (
                [lid for lid in line_id if lid in allowed_set]
                if line_id
                else allowed_line_ids
            )
        async with async_session_factory() as session:
            dao = SysArchiveDao(session=session)
            return await dao.get_grouped(from_date, to_date, line_id)


sys_router = SysArchiveRouter().router
