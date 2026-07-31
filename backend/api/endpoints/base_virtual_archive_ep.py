"""
Shared router for archive endpoints with virtual-line (ring) support.

Both /hourly_virtual/ and /daily_virtual/ behave identically: resolve virtual
line IDs to their physical members, query the archive DAO, and aggregate back
into virtual rows when a ring was requested. The subclasses only pick the DAO,
path and date-range cap (mirrors the BaseArchiveRouter pattern).
"""

from datetime import datetime
from types import SimpleNamespace
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.api.endpoints.auth_ep import get_current_user
from backend.db.dao.dpd_line_dao import DpdLineArchiveDao
from backend.db.engine import get_session
from backend.db.models.dpd_line_model import DpdLine
from backend.services.virtual_lines_aggregator import aggregate_to_virtual_lines
from backend.services.virtual_lines_config import (
    get_active_virtual_lines_db,
    resolve_virtual_to_physical_db,
)


class BaseVirtualArchiveRouter:
    def __init__(self, path: str, tag: str, archive_dao, period_type: str):
        self.archive_dao = archive_dao
        # "daily" | "hourly" — which DPD-line archive feeds DPD ring members.
        self.period_type = period_type
        self.router = APIRouter(dependencies=[Depends(get_current_user)])
        self.router.add_api_route(
            path=path,
            tags=[tag],
            endpoint=self.get_archive,
            methods=["GET"],
            response_model=List[dict],
            status_code=status.HTTP_200_OK,
            summary=f"Get {tag} archives with virtual lines support",
            description=(
                "Returns archive data supporting both physical and virtual lines. "
                "Virtual line IDs are automatically aggregated from their "
                "constituent physical lines."
            ),
        )

    async def get_archive(
        self,
        from_date: datetime = Query(None, description="Start date/time"),
        to_date: datetime = Query(None, description="End date/time"),
        line_id: List[int] = Query(None, description="List of line IDs (virtual IDs supported)"),
        session: AsyncSession = Depends(get_session),
    ):
        # Both ends required, no cap on the length — see BaseArchiveRouter.
        if not from_date or not to_date:
            raise HTTPException(status_code=400, detail="Вкажіть початок і кінець періоду")

        if not line_id:
            line_id = []

        virtual_lines = await get_active_virtual_lines_db(session)
        physical_line_ids = await resolve_virtual_to_physical_db(line_id, session)
        archive_dao = self.archive_dao(session=session)
        archives = list(
            await archive_dao.get_range(from_date, to_date, physical_line_ids)
        )

        # Resolved member ids may include DPD lines (a ring can mix kinds);
        # their data lives in the dpd_line_* archives, not in hourly/daily
        # archive — fetch and merge before aggregation. Shims mirror the
        # archive-row attributes the aggregator reads.
        dpd_ids = list((await session.execute(
            select(DpdLine.id).where(DpdLine.id.in_(physical_line_ids))
        )).scalars()) if physical_line_ids else []
        if dpd_ids:
            dpd_rows = await DpdLineArchiveDao(session).load_range(
                self.period_type, dpd_ids, from_date, to_date
            )
            archives.extend(
                SimpleNamespace(
                    line_id=r["dpd_line_id"],
                    period=r["stamp"],
                    volume=r["volume"],
                    w_volume_dp=None,
                    pressure=r["pressure"],
                    temperature=r["temperature"],
                    density=None,
                )
                for r in dpd_rows
            )

        has_virtual = any(str(lid) in virtual_lines for lid in line_id)
        if has_virtual:
            return aggregate_to_virtual_lines(archives, line_id, virtual_lines=virtual_lines)

        return [
            {
                "line_id": archive.line_id,
                "period": archive.period,
                "volume": archive.volume,
                "w_volume_dp": archive.w_volume_dp,
                "pressure": archive.pressure,
                "temperature": archive.temperature,
                "density": archive.density,
            }
            for archive in archives
        ]
