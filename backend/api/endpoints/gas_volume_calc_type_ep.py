import json
from pathlib import Path

from fastapi import APIRouter, Depends, status, HTTPException
from sqlalchemy import text
from sqlmodel import select

from backend.api.endpoints.auth_ep import get_current_user
from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.dao.gas_volume_calc_type_dao import GasVolumeCalcTypeDao
from backend.db.engine import DbEngine, async_session_factory
from backend.db.models import GasVolumeCalcTypeCreate, GasVolumeCalcTypeList
from backend.db.models.gas_volume_calc_type_model import GasVolumeCalcType, GasVolumeCalcTypeUpdate
from backend.db.models.sys_type_model import SysType
from backend.db.models.edit_type_model import EditType
from backend.db.models.app_user_model import AppUser


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
        self.router.add_api_route(
            path="/gas-volume-calc-types/export-preload",
            tags=["Gas volume types"],
            endpoint=self.export_preload_json,
            methods=["POST"],
            status_code=status.HTTP_200_OK,
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


    async def export_preload_json(self, user: AppUser = Depends(get_current_user)):
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Admin only")

        async with async_session_factory() as session:
            calc_types = (await session.execute(
                select(GasVolumeCalcType).order_by(GasVolumeCalcType.type_id)
            )).scalars().all()
            sys_types = (await session.execute(
                select(SysType).order_by(SysType.gas_volume_calc_type_id, SysType.sys_type_id)
            )).scalars().all()
            edit_types = (await session.execute(
                select(EditType).order_by(EditType.gas_volume_calc_type_id, EditType.edit_type_id)
            )).scalars().all()

        base = Path("backend/db/preload_db")
        flowtype = {"FLOWTYPE": [{"ID_TYPE": c.type_id, "TYPENAME": c.type_name} for c in calc_types]}
        sysname  = {"SYSNAME":  [{"ID_TYPE": s.gas_volume_calc_type_id, "SYS_ID":  s.sys_type_id,  "SYSNAME":  s.sys_name}  for s in sys_types]}
        editname = {"EDITNAME": [{"ID_TYPE": e.gas_volume_calc_type_id, "EDIT_ID": e.edit_type_id, "EDITNAME": e.edit_name} for e in edit_types]}

        (base / "FLOWTYPE.json").write_text(json.dumps(flowtype,  ensure_ascii=False, indent=2), encoding="utf-8")
        (base / "SYSNAME.json").write_text( json.dumps(sysname,   ensure_ascii=False, indent=2), encoding="utf-8")
        (base / "EDITNAME.json").write_text(json.dumps(editname,  ensure_ascii=False, indent=2), encoding="utf-8")

        return {"ok": True, "exported": {
            "flowtype": len(calc_types),
            "sysname":  len(sys_types),
            "editname": len(edit_types),
        }}


gvct_router = GasVolumeCalcTypeRouter().router
