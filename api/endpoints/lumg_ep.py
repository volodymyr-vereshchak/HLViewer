from fastapi import APIRouter, status, HTTPException

from backend.db.dao.lumg_dao import LumgDao
from backend.db.models import LumgCreate, LumgList
from backend.db.models.lumg_model import LumgUpdate

router = APIRouter()

@router.get("/lumgs/", response_model=list[LumgList], status_code=status.HTTP_200_OK)
async def get_lumgs():
    lumgs = LumgDao().get_all()
    return lumgs

@router.post("/lumgs/", response_model=LumgCreate, status_code=status.HTTP_201_CREATED)
async def create_lumg(lumg: LumgCreate):
    lumg = LumgDao().create_item(lumg)
    return lumg

@router.patch("/lumgs/{lumg_id}", response_model=LumgList, status_code=status.HTTP_202_ACCEPTED)
async def update_lumg(lumg_id: int, lumg: LumgUpdate):
    lumg_db = LumgDao().update_by_id(lumg_id, lumg)
    if not lumg_db:
        raise HTTPException(status_code=404, detail="Hero not found")
    return lumg_db
