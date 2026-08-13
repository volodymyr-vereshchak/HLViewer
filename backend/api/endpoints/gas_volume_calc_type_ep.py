from fastapi import APIRouter, Depends, status, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.endpoints.auth_ep import get_current_user, require_admin
from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.dao.gas_volume_calc_type_dao import GasVolumeCalcTypeDao
from backend.db.engine import get_session
from backend.db.models import GasVolumeCalcTypeCreate, GasVolumeCalcTypeList
from backend.db.models.gas_volume_calc_type_model import GasVolumeCalcType, GasVolumeCalcTypeUpdate
from backend.db.models.app_user_model import AppUser
from backend.db.preload_db.event_types_json import export_event_types, import_event_types


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
        self.router.add_api_route(
            path="/gas-volume-calc-types/preload",
            tags=["Gas volume types"],
            endpoint=self.preload_json,
            methods=["POST"],
            status_code=status.HTTP_200_OK,
        )

    async def get_gvct(self, session: AsyncSession = Depends(get_session)):
        gvct = await GasVolumeCalcTypeDao(session=session).get_all()
        return gvct

    async def create_gvct(
        self, gvct: GasVolumeCalcTypeCreate, session: AsyncSession = Depends(get_session)
    ):
        try:
            gvct = await GasVolumeCalcTypeDao(session=session).create_item(gvct)
        except DatabaseIntegrityError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return gvct

    async def update_gvct(
        self,
        gvct_id: int,
        gvct: GasVolumeCalcTypeUpdate,
        session: AsyncSession = Depends(get_session),
    ):
        gvct_db = await GasVolumeCalcTypeDao(session=session).update_by_id(
            gvct_id, gvct
        )
        if not gvct_db:
            raise HTTPException(
                status_code=404, detail="Type of gas volume calc not found"
            )
        return gvct_db

    async def delete_gvct(self, gvct_id: int, session: AsyncSession = Depends(get_session)):
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


    async def export_preload_json(
        self,
        user: AppUser = Depends(require_admin),
        session: AsyncSession = Depends(get_session),
    ):
        """DB → FLOWTYPE/SYSNAME/EDITNAME.json. The files are committed with the
        code and reloaded on every start, so this is what makes an edit made in
        the admin panel survive a restart and reach the offline server."""
        counts = await export_event_types(session)
        return {"ok": True, "exported": {
            "flowtype": counts.flowtype,
            "sysname": counts.sysname,
            "editname": counts.editname,
        }}

    async def preload_json(
        self,
        force: bool = False,
        user: AppUser = Depends(require_admin),
        session: AsyncSession = Depends(get_session),
    ):
        """The other direction. Without `force` a merge; with it sys_type and
        edit_type are emptied first — see `import_event_types` for why the
        calculator types are never wiped."""
        counts = await import_event_types(session, force=force)
        return {"ok": True, "wiped": counts.wiped, "exported": {
            "flowtype": counts.flowtype,
            "sysname": counts.sysname,
            "editname": counts.editname,
        }}


gvct_router = GasVolumeCalcTypeRouter().router
