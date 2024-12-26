from fastapi import APIRouter, status

from backend.db.dao.daily_archive_dao import DailyArchiveDao
from backend.db.models import DailyArchiveList
from backend.main import update_hostlibs

router = APIRouter()

@router.get("/day_archive/", response_model=list[DailyArchiveList])
async def get_day_archive():
    daily_archives = DailyArchiveDao().get_all()
    return daily_archives

@router.patch("/day_archive/", status_code=status.HTTP_201_CREATED)
async def update_day_archive(path: str):
    update_hostlibs(path)
