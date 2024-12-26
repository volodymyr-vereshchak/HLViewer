from fastapi import FastAPI, status

from backend.db.dao.daily_archive_dao import DailyArchiveDao
from backend.db.dao.lumg_dao import LumgDao
from backend.db.models import DailyArchiveList, LumgCreate

app = FastAPI()

@app.get("/")
async def root():
    return {"message": "Hello World"}

@app.get("/day_archive/", response_model=list[DailyArchiveList])
async def get_day_archive():
    daily_archives = DailyArchiveDao().get_all()
    return daily_archives

@app.post("/lumgs/", response_model=LumgCreate, status_code=status.HTTP_201_CREATED)
async def create_lumg(lumg: LumgCreate):
    lumg = LumgDao().create_item(lumg)
    return lumg
