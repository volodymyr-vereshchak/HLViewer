from fastapi import APIRouter, status

from backend.db.dao.daily_archive_dao import DailyArchiveDao
from backend.db.models import DailyArchiveList

# router = APIRouter()
#
# @router.get("/day_archive/", response_model=list[DailyArchiveList])
# async def get_day_archive():
#     daily_archives = DailyArchiveDao().get_all()
#     return daily_archives

class DailyArchiveRouter:
    def __init__(self):
        self.router = APIRouter()
        self.router.add_api_route(
            path="/day_archive/",
            endpoint=self.get_day_archive,
            tags=["daily"],
            methods=["GET"],
            status_code=status.HTTP_200_OK
        )

    async def get_day_archive(self):
        daily_archives = DailyArchiveDao().get_all()
        return daily_archives

daily_router = DailyArchiveRouter().router
