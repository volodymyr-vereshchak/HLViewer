from fastapi import APIRouter, Depends, status, HTTPException
from sqlmodel import select

from backend.api.endpoints.auth_ep import get_branch_filter
from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.dao.gas_volume_calc_dao import GasVolumeCalcDao
from backend.db.dao.gas_volume_calc_type_dao import GasVolumeCalcTypeDao
from backend.db.dao.line_dao import LineDao
from backend.db.engine import DbEngine, async_session_factory
from backend.db.models import (
    GasVolumeCalcList,
    GasVolumeCalcCreate,
    LineList,
    LineCreate,
    LineUpdate,
)
from backend.db.models.gas_volume_calc_model import GasVolumeCalc, GasVolumeCalcUpdate
from backend.db.models.line_model import Line
from backend.db.models.lumg_model import Lumg


class LineRouter:
    def __init__(self):
        self.router = APIRouter()
        self.router.add_api_route(
            path="/lines/",
            tags=["lines"],
            endpoint=self.get_lines,
            methods=["GET"],
            response_model=list[LineList],
            status_code=status.HTTP_200_OK,
        )
        self.router.add_api_route(
            path="/lines/",
            tags=["lines"],
            endpoint=self.create_line,
            methods=["POST"],
            response_model=LineCreate,
            status_code=status.HTTP_201_CREATED,
        )
        self.router.add_api_route(
            path="/lines/{line_id}",
            tags=["lines"],
            endpoint=self.get_line_by_id,
            methods=["GET"],
            response_model=LineList,
            status_code=status.HTTP_200_OK,
        )
        self.router.add_api_route(
            path="/lines/{line_id}",
            tags=["lines"],
            endpoint=self.update_line,
            methods=["PATCH"],
            response_model=LineList,
            status_code=status.HTTP_202_ACCEPTED,
        )

        self.router.add_api_route(
            path="/lines/{line_id}",
            tags=["lines"],
            endpoint=self.delete_line,
            methods=["DELETE"],
            status_code=status.HTTP_204_NO_CONTENT,
        )

    async def get_lines(
        self,
        lumg_id: int = None,
        include_in_trends: bool = None,
        branch_ids: list[int] | None = Depends(get_branch_filter),
    ):
        async with async_session_factory() as session:
            if branch_ids is None and lumg_id is None and include_in_trends is None:
                lines = await LineDao(session=session).get_all()
                return lines

            stmt = (
                select(Line)
                .join(GasVolumeCalc, Line.gas_volume_calc_id == GasVolumeCalc.id)
                .join(Lumg, GasVolumeCalc.lumg_id == Lumg.id)
            )
            if lumg_id is not None:
                stmt = stmt.where(GasVolumeCalc.lumg_id == lumg_id)
            if branch_ids is not None:
                stmt = stmt.where(Lumg.branch_id.in_(branch_ids))
            if include_in_trends is not None:
                stmt = stmt.where(Line.include_in_trends == include_in_trends)
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_line_by_id(self, line_id: int):
        async with async_session_factory() as session:
            line = await LineDao(session=session).get_by_id(line_id)
        if not line:
            raise HTTPException(status_code=404, detail="Line not found")
        return line

    async def create_line(self, line: LineCreate):
        try:
            async with async_session_factory() as session:
                line_db = await LineDao(session=session).create_item(line)
        except DatabaseIntegrityError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return line_db

    async def update_line(self, line_id: int, line: LineUpdate):
        async with async_session_factory() as session:
            line_db = await LineDao(session=session).update_by_id(line_id, line)
        if not line_db:
            raise HTTPException(status_code=404, detail="Line not found")
        return line_db

    async def delete_line(self, line_id: int):
        async with async_session_factory() as session:
            delete_line = await LineDao(session=session).delete_item(line_id)
        if not delete_line:
            raise HTTPException(status_code=404, detail="Line not found")
        return {"ok": True}


line_router = LineRouter().router
