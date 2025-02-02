from datetime import datetime

from fastapi import APIRouter, status, Query

from backend.db.dao.daily_archive_dao import DailyArchiveDao
from backend.db.models import DailyArchiveList


class DailyArchiveRouter:
    def __init__(self):
        self.router = APIRouter()
        self.router.add_api_route(
            path="/day_archive/",
            endpoint=self.get_day_archive,
            response_model=list[DailyArchiveList],
            tags=["daily"],
            methods=["GET"],
            status_code=status.HTTP_200_OK,
        )

    async def get_day_archive(
        self,
        from_date: datetime = Query(None),
        to_date: datetime = Query(None),
        line_id: list[int] = Query(None),
    ):
        daily_archives_dao = DailyArchiveDao()
        daily_archives = daily_archives_dao.get_range(from_date, to_date, line_id)

        return daily_archives


daily_router = DailyArchiveRouter().router
