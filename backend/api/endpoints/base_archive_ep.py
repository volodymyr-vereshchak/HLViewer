from datetime import datetime

from fastapi import APIRouter, status, Query
from backend.db.engine import DbEngine, async_session_factory


class BaseArchiveRouter:
    def __init__(self, path: str, archive_list_class, tags: list[str], archive_dao):
        self.router = APIRouter()
        self.archive_dao = archive_dao
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
    ):
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
