from datetime import datetime

from fastapi import APIRouter, status

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
        self, from_date: datetime = None, to_date: datetime = None
    ):
        daily_archives_dao = DailyArchiveDao()
        if from_date and to_date:
            daily_archives = daily_archives_dao.get_range(from_date, to_date)
        else:
            daily_archives = daily_archives_dao.get_all()
        return daily_archives


daily_router = DailyArchiveRouter().router
