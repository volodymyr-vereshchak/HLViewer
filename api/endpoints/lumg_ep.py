from fastapi import APIRouter, status, HTTPException

from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.dao.lumg_dao import LumgDao
from backend.db.models import LumgCreate, LumgList
from backend.db.models.lumg_model import LumgUpdate


class LumgRouter:
    def __init__(self):
        self.router = APIRouter()
        self.router.add_api_route(
            path="/lumgs/",
            tags=["lumg"],
            endpoint=self.get_lumgs,
            methods=["GET"],
            response_model=list[LumgList],
            status_code=status.HTTP_200_OK,
        )
        self.router.add_api_route(
            path="/lumgs/",
            tags=["lumg"],
            endpoint=self.create_lumg,
            methods=["POST"],
            response_model=LumgCreate,
            status_code=status.HTTP_201_CREATED,
        )
        self.router.add_api_route(
            path="/lumgs/{lumg_id}",
            tags=["lumg"],
            endpoint=self.update_lumg,
            methods=["PATCH"],
            response_model=LumgList,
            status_code=status.HTTP_202_ACCEPTED,
        )

        self.router.add_api_route(
            path="/lumgs/{lumg_id}",
            tags=["lumg"],
            endpoint=self.delete_lumg,
            methods=["DELETE"],
            status_code=status.HTTP_204_NO_CONTENT,
        )

    async def get_lumgs(self):
        lumgs = LumgDao().get_all()
        return lumgs

    async def create_lumg(self, lumg: LumgCreate):
        try:
            lumg = LumgDao().create_item(lumg)
        except DatabaseIntegrityError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return lumg

    async def update_lumg(self, lumg_id: int, lumg: LumgUpdate):
        lumg_db = LumgDao().update_by_id(lumg_id, lumg)
        if not lumg_db:
            raise HTTPException(status_code=404, detail="Lumg not found")
        return lumg_db

    async def delete_lumg(self, lumg_id: int):
        delete_lumg = LumgDao().delete_item(lumg_id)
        if not delete_lumg:
            raise HTTPException(status_code=404, detail="Lumg not found")
        return {"ok": True}

lumg_router = LumgRouter().router
