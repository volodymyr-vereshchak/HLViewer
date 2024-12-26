from fastapi import APIRouter

from backend.db.dao.daily_archive_dao import DailyArchiveDao
from backend.db.models import DailyArchiveList

router = APIRouter()

@router.get("/day_archive/", response_model=list[DailyArchiveList])
async def get_day_archive():
    daily_archives = DailyArchiveDao().get_all()
    return daily_archives
