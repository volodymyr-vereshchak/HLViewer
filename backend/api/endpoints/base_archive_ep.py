from datetime import datetime

from fastapi import APIRouter, status, Query


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

    async def get_archive(
        self,
        from_date: datetime = Query(None),
        to_date: datetime = Query(None),
        line_id: list = Query(None),
    ):
        archive_dao = self.archive_dao()
        archives = archive_dao.get_range(from_date, to_date, line_id)
        return archives
