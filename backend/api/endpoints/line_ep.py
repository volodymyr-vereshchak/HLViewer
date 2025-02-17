from fastapi import APIRouter, status, HTTPException

from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.dao.gas_volume_calc_dao import GasVolumeCalcDao
from backend.db.dao.gas_volume_calc_type_dao import GasVolumeCalcTypeDao
from backend.db.dao.line_dao import LineDao
from backend.db.engine import DbEngine
from backend.db.models import (
    GasVolumeCalcList,
    GasVolumeCalcCreate,
    LineList,
    LineCreate,
    LineUpdate,
)
from backend.db.models.gas_volume_calc_model import GasVolumeCalcUpdate


class LineRouter:
    def __init__(self):
        self.router = APIRouter()
        self.session_factory = DbEngine().get_session()
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

    async def get_lines(self, lumg_id: int = None):
        async with self.session_factory as session:
            lines = await LineDao(session=session).get_line_by_lumg_id(lumg_id)
        return lines

    async def create_line(self, line: LineCreate):
        try:
            async with self.session_factory as session:
                line_db = await LineDao(session=session).create_item(line)
        except DatabaseIntegrityError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return line_db

    async def update_line(self, line_id: int, line: LineUpdate):
        async with self.session_factory as session:
            line_db = await LineDao(session=session).update_by_id(line_id, line)
        if not line_db:
            raise HTTPException(status_code=404, detail="Gas volume calc not found")
        return line_db

    async def delete_line(self, line_id: int):
        async with self.session_factory as session:
            delete_line = await LineDao(session=session).delete_item(line_id)
        if not delete_line:
            raise HTTPException(status_code=404, detail="Gas volume calc not found")
        return {"ok": True}


line_router = LineRouter().router
