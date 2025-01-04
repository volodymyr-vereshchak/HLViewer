from fastapi import APIRouter, status, HTTPException

from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.dao.gas_volume_calc_type_dao import GasVolumeCalcTypeDao
from backend.db.models import GasVolumeCalcTypeCreate, GasVolumeCalcTypeList
from backend.db.models.gas_volume_calc_type_model import GasVolumeCalcTypeUpdate


class GasVolumeCalcTypeRouter:
    def __init__(self):
        self.router = APIRouter()
        self.router.add_api_route(
            path="/gas_volume_calc_types/",
            tags=["Gas volume types"],
            endpoint=self.get_gvct,
            methods=["GET"],
            response_model=list[GasVolumeCalcTypeList],
            status_code=status.HTTP_200_OK,
        )
        self.router.add_api_route(
            path="/gas_volume_calc_types/",
            tags=["Gas volume types"],
            endpoint=self.create_gvct,
            methods=["POST"],
            response_model=GasVolumeCalcTypeCreate,
            status_code=status.HTTP_201_CREATED,
        )
        self.router.add_api_route(
            path="/gas_volume_calc_types/{gvct_id}",
            tags=["Gas volume types"],
            endpoint=self.update_gvct,
            methods=["PATCH"],
            response_model=GasVolumeCalcTypeList,
            status_code=status.HTTP_202_ACCEPTED,
        )

        self.router.add_api_route(
            path="/gas_volume_calc_types/{gvct_id}",
            tags=["Gas volume types"],
            endpoint=self.delete_gvct,
            methods=["DELETE"],
            status_code=status.HTTP_204_NO_CONTENT,
        )

    async def get_gvct(self):
        gvct = GasVolumeCalcTypeDao().get_all()
        return gvct

    async def create_gvct(self, gvct: GasVolumeCalcTypeCreate):
        try:
            gvct = GasVolumeCalcTypeDao().create_item(gvct)
        except DatabaseIntegrityError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return gvct

    async def update_gvct(self, gvct_id: int, gvct: GasVolumeCalcTypeUpdate):
        gvct_db = GasVolumeCalcTypeDao().update_by_id(gvct_id, gvct)
        if not gvct_db:
            raise HTTPException(status_code=404, detail="Type of gas volume calc not found")
        return gvct_db

    async def delete_gvct(self, gvct_id: int):
        delete_gvct = GasVolumeCalcTypeDao().delete_item(gvct_id)
        if not delete_gvct:
            raise HTTPException(status_code=404, detail="Type of gas volume calc not found")
        return {"ok": True}

gvct_router = GasVolumeCalcTypeRouter().router
