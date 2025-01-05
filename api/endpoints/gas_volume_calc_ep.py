from fastapi import APIRouter, status, HTTPException

from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.dao.gas_volume_calc_dao import GasVolumeCalcDao
from backend.db.dao.gas_volume_calc_type_dao import GasVolumeCalcTypeDao
from backend.db.models import GasVolumeCalcList, GasVolumeCalcCreate
from backend.db.models.gas_volume_calc_model import GasVolumeCalcUpdate


class GasVolumeCalcRouter:
    def __init__(self):
        self.router = APIRouter()
        self.router.add_api_route(
            path="/gas_volume_calcs/",
            tags=["Gas volume calcs"],
            endpoint=self.get_gas_volume_calc,
            methods=["GET"],
            response_model=list[GasVolumeCalcList],
            status_code=status.HTTP_200_OK,
        )
        self.router.add_api_route(
            path="/gas_volume_calcs/",
            tags=["Gas volume calcs"],
            endpoint=self.create_gas_volume_calc,
            methods=["POST"],
            response_model=GasVolumeCalcCreate,
            status_code=status.HTTP_201_CREATED,
        )
        self.router.add_api_route(
            path="/gas_volume_calcs/{gas_volume_calc_id}",
            tags=["Gas volume calcs"],
            endpoint=self.update_gas_volume_calc,
            methods=["PATCH"],
            response_model=GasVolumeCalcList,
            status_code=status.HTTP_202_ACCEPTED,
        )

        self.router.add_api_route(
            path="/gas_volume_calcs/{gas_volume_calc_id}",
            tags=["Gas volume calcs"],
            endpoint=self.delete_gas_volume_calc,
            methods=["DELETE"],
            status_code=status.HTTP_204_NO_CONTENT,
        )

    async def get_gas_volume_calc(self):
        gas_volume_calc = GasVolumeCalcDao().get_all()
        return gas_volume_calc

    async def create_gas_volume_calc(self, gvc: GasVolumeCalcCreate):
        try:
            gas_volume_calc = GasVolumeCalcDao().create_item(gvc)
        except DatabaseIntegrityError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return gas_volume_calc

    async def update_gas_volume_calc(self, gas_volume_calc_id: int, gas_volume_calc: GasVolumeCalcUpdate):
        gas_volume_calc_db = GasVolumeCalcDao().update_by_id(gas_volume_calc_id, gas_volume_calc)
        if not gas_volume_calc_db:
            raise HTTPException(status_code=404, detail="Gas volume calc not found")
        return gas_volume_calc_db

    async def delete_gas_volume_calc(self, gas_volume_calc_id: int):
        delete_gas_volume_calc = GasVolumeCalcTypeDao().delete_item(gas_volume_calc_id)
        if not delete_gas_volume_calc:
            raise HTTPException(status_code=404, detail="Gas volume calc not found")
        return {"ok": True}

gas_volume_calc_router = GasVolumeCalcRouter().router
