from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status, Query
from backend.api.endpoints.auth_ep import get_allowed_line_ids
from backend.db.engine import DbEngine, async_session_factory


class BaseArchiveRouter:
    def __init__(self, path: str, archive_list_class, tags: list[str], archive_dao, max_days: int = 400):
        self.router = APIRouter()
        self.archive_dao = archive_dao
        self.max_days = max_days
        self.router.add_api_route(
            path=path,
            endpoint=self.get_archive,
            response_model=list[archive_list_class],
            tags=tags,
            methods=["GET"],
            status_code=status.HTTP_200_OK,
        )
        self.router.add_api_route(
            path=path[:-1] + "_counts/",
            endpoint=self.get_archive_counts,
            tags=tags,
            methods=["GET"],
            status_code=status.HTTP_200_OK,
        )

    async def get_archive(
        self,
        from_date: datetime = Query(None),
        to_date: datetime = Query(None),
        line_id: list[int] = Query(None),
        allowed_line_ids: list[int] | None = Depends(get_allowed_line_ids),
    ):
        if not from_date or not to_date:
            raise HTTPException(status_code=400, detail="from_date and to_date are required")
        if (to_date - from_date).days > self.max_days:
            raise HTTPException(status_code=400, detail=f"Date range exceeds {self.max_days} days")
        if allowed_line_ids is not None:
            allowed_set = set(allowed_line_ids)
            line_id = [lid for lid in line_id if lid in allowed_set] if line_id else allowed_line_ids
        async with async_session_factory() as session:
            archive_dao = self.archive_dao(session=session)
            archives = await archive_dao.get_range(from_date, to_date, line_id)
            return archives

    async def get_archive_counts(
        self,
        from_date: datetime = Query(None),
        to_date: datetime = Query(None),
        line_id: list[int] = Query(None),
    ):
        async with async_session_factory() as session:
            return await self.archive_dao(session=session).get_data_counts_by_hour(
                from_date=from_date, to_date=to_date, line_id=line_id
            )
