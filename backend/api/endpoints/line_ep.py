from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query, status, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.api.endpoints.auth_ep import get_branch_filter
from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.dao.line_dao import LineDao
from backend.db.engine import get_session
from backend.db.models import (
    LineList,
    LineCreate,
    LineUpdate,
)
from backend.db.models.gas_volume_calc_model import GasVolumeCalc
from backend.db.models.line_model import Line
from backend.db.models.lumg_model import Lumg
from backend.services import line_archive_cleanup


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

        self.router.add_api_route(
            path="/lines/{line_id}/archive/preview",
            tags=["lines"],
            endpoint=self.preview_archive_purge,
            methods=["GET"],
            status_code=status.HTTP_200_OK,
            summary="How much of this line's archive a range holds",
            description=(
                "Counts by kind — добові, годинні, зміни, аварії, параметри — "
                "plus the first and last day the line has anything on. Either "
                "end of the range may be left out: only a start means "
                "everything from that day on, only an end means everything up "
                "to it, both mean the range between them, ends included."
            ),
        )
        self.router.add_api_route(
            path="/lines/{line_id}/archive",
            tags=["lines"],
            endpoint=self.purge_archive,
            methods=["DELETE"],
            status_code=status.HTTP_200_OK,
            summary="Remove this line's archive over a range",
            description=(
                "All five archives of the line at once, over the same range as "
                "the preview. Irreversible: what goes comes back only by "
                "reading the hostlib files again, which is the update asked "
                "for by hand on that path. Admin-only."
            ),
        )

    async def get_lines(
        self,
        lumg_id: int = None,
        include_in_trends: bool = None,
        branch_ids: list[int] | None = Depends(get_branch_filter),
        session: AsyncSession = Depends(get_session),
    ):
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
        # Stable order: heap order changes whenever a row is updated.
        stmt = stmt.order_by(Line.id)
        result = await session.execute(stmt)
        return result.scalars().all()

    async def get_line_by_id(self, line_id: int, session: AsyncSession = Depends(get_session)):
        line = await LineDao(session=session).get_by_id(line_id)
        if not line:
            raise HTTPException(status_code=404, detail="Line not found")
        return line

    async def create_line(self, line: LineCreate, session: AsyncSession = Depends(get_session)):
        try:
            line_db = await LineDao(session=session).create_item(line)
        except DatabaseIntegrityError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return line_db

    async def update_line(self, line_id: int, line: LineUpdate, session: AsyncSession = Depends(get_session)):
        line_db = await LineDao(session=session).update_by_id(line_id, line)
        if not line_db:
            raise HTTPException(status_code=404, detail="Line not found")
        return line_db

    async def _line_or_404(self, line_id: int, session: AsyncSession) -> Line:
        line = await session.get(Line, line_id)
        if not line:
            raise HTTPException(status_code=404, detail="Line not found")
        return line

    @staticmethod
    def _range(since: Optional[date], until: Optional[date],
               removing: bool) -> None:
        """Refuse a range that cannot be used.

        Counting has no lower bar than making sense: with neither date it says
        what the whole archive holds, which is what the screen opens with.
        Removing does — two empty fields must not clear a line's history.
        """
        refusal = line_archive_cleanup.valid(since, until)
        if refusal and (removing or since is not None or until is not None):
            raise HTTPException(status_code=400, detail=refusal)

    async def preview_archive_purge(
        self,
        line_id: int,
        from_date: Optional[date] = Query(
            None, description="Перший день, включно. Порожньо — від початку архіву"),
        to_date: Optional[date] = Query(
            None, description="Останній день, включно. Порожньо — до кінця архіву"),
        session: AsyncSession = Depends(get_session),
    ):
        """What clearing this range would take, counted by kind."""
        line = await self._line_or_404(line_id, session)
        self._range(from_date, to_date, removing=False)
        return {
            "line_id": line.id,
            "name": line.name,
            "counts": await line_archive_cleanup.counts(
                session, line_id, from_date, to_date),
            "extent": await line_archive_cleanup.extent(session, line_id),
        }

    async def purge_archive(
        self,
        line_id: int,
        from_date: Optional[date] = Query(None),
        to_date: Optional[date] = Query(None),
        session: AsyncSession = Depends(get_session),
    ):
        """Remove this line's archive over the range. Counted first by the
        screen that calls it — see preview_archive_purge."""
        await self._line_or_404(line_id, session)
        self._range(from_date, to_date, removing=True)
        removed = await line_archive_cleanup.purge(
            session, line_id, from_date, to_date)
        await session.commit()
        return {"removed": removed}

    async def delete_line(self, line_id: int, session: AsyncSession = Depends(get_session)):
        exists = await session.get(Line, line_id)
        if not exists:
            raise HTTPException(status_code=404, detail="Line not found")
        await session.execute(text("DELETE FROM gas_volume_line WHERE id = :id"), {"id": line_id})
        await session.commit()


line_router = LineRouter().router
