from fastapi import APIRouter, Depends, status, HTTPException
from sqlalchemy import text

from backend.api.endpoints.auth_ep import get_current_user
from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.dao.gas_volume_calc_type_dao import GasVolumeCalcTypeDao
from backend.db.engine import DbEngine, async_session_factory
from backend.db.models import GasVolumeCalcTypeCreate, GasVolumeCalcTypeList
from backend.db.models.gas_volume_calc_type_model import GasVolumeCalcType, GasVolumeCalcTypeUpdate


class GasVolumeCalcTypeRouter:
    def __init__(self):
        self.router = APIRouter(dependencies=[Depends(get_current_user)])
        self.router.add_api_route(
            path="/gas-volume-calc-types/",
            tags=["Gas volume types"],
            endpoint=self.get_gvct,
            methods=["GET"],
            response_model=list[GasVolumeCalcTypeList],
            status_code=status.HTTP_200_OK,
        )
        self.router.add_api_route(
            path="/gas-volume-calc-types/",
            tags=["Gas volume types"],
            endpoint=self.create_gvct,
            methods=["POST"],
            response_model=GasVolumeCalcTypeCreate,
            status_code=status.HTTP_201_CREATED,
        )
        self.router.add_api_route(
            path="/gas-volume-calc-types/{gvct_id}",
            tags=["Gas volume types"],
            endpoint=self.update_gvct,
            methods=["PATCH"],
            response_model=GasVolumeCalcTypeList,
            status_code=status.HTTP_202_ACCEPTED,
        )

        self.router.add_api_route(
            path="/gas-volume-calc-types/{gvct_id}",
            tags=["Gas volume types"],
            endpoint=self.delete_gvct,
            methods=["DELETE"],
            status_code=status.HTTP_204_NO_CONTENT,
        )

    async def get_gvct(self):
        async with async_session_factory() as session:
            gvct = await GasVolumeCalcTypeDao(session=session).get_all()
        return gvct

    async def create_gvct(self, gvct: GasVolumeCalcTypeCreate):
        try:
            async with async_session_factory() as session:
                gvct = await GasVolumeCalcTypeDao(session=session).create_item(gvct)
        except DatabaseIntegrityError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return gvct

    async def update_gvct(self, gvct_id: int, gvct: GasVolumeCalcTypeUpdate):
        async with async_session_factory() as session:
            gvct_db = await GasVolumeCalcTypeDao(session=session).update_by_id(
                gvct_id, gvct
            )
        if not gvct_db:
            raise HTTPException(
                status_code=404, detail="Type of gas volume calc not found"
            )
        return gvct_db

    async def delete_gvct(self, gvct_id: int):
        async with async_session_factory() as session:
            exists = await session.get(GasVolumeCalcType, gvct_id)
            if not exists:
                raise HTTPException(
                    status_code=404, detail="Type of gas volume calc not found"
                )
            # Raw SQL so PostgreSQL handles CASCADE at DB level (not ORM which
            # would load all related GasVolumeCalc → Line → archive rows into memory)
            await session.execute(
                text("DELETE FROM gas_vol_calc_type WHERE id = :id"), {"id": gvct_id}
            )
            await session.commit()
        return {"ok": True}


gvct_router = GasVolumeCalcTypeRouter().router
