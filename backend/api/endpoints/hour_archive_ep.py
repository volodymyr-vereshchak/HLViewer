from datetime import datetime

from fastapi import APIRouter, status, Query
from sqlmodel import select

from backend.db.dao.hourly_archive_dao import HourlyArchiveDao
from backend.db.models import HourlyArchiveList, HourlyArchive


class HourlyArchiveRouter:
    def __init__(self):
        self.router = APIRouter()
        self.router.add_api_route(
            path="/hour_archive/",
            endpoint=self.get_hour_archive,
            response_model=list[HourlyArchiveList],
            tags=["hourly"],
            methods=["GET"],
            status_code=status.HTTP_200_OK,
        )

    async def get_hour_archive(
        self,
        from_date: datetime = Query(None),
        to_date: datetime = Query(None),
        line_id: list = Query(None),
    ):
        hourly_archives_dao = HourlyArchiveDao()
        hourly_archives = hourly_archives_dao.get_range(from_date, to_date, line_id)
        return hourly_archives


hourly_router = HourlyArchiveRouter().router
