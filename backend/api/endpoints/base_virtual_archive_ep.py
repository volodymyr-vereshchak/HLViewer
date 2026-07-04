"""
Shared router for archive endpoints with virtual-line (ring) support.

Both /hourly_virtual/ and /daily_virtual/ behave identically: resolve virtual
line IDs to their physical members, query the archive DAO, and aggregate back
into virtual rows when a ring was requested. The subclasses only pick the DAO,
path and date-range cap (mirrors the BaseArchiveRouter pattern).
"""

from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.api.endpoints.auth_ep import get_current_user
from backend.db.engine import async_session_factory
from backend.services.virtual_lines_aggregator import aggregate_to_virtual_lines
from backend.services.virtual_lines_config import (
    get_active_virtual_lines_db,
    resolve_virtual_to_physical_db,
)


class BaseVirtualArchiveRouter:
    def __init__(self, path: str, tag: str, archive_dao, max_days: int):
        self.archive_dao = archive_dao
        self.max_days = max_days
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
    ):
        if not from_date or not to_date:
            raise HTTPException(status_code=400, detail="from_date and to_date are required")
        if (to_date - from_date).days > self.max_days:
            raise HTTPException(status_code=400, detail=f"Date range exceeds {self.max_days} days")

        if not line_id:
            line_id = []

        async with async_session_factory() as session:
            virtual_lines = await get_active_virtual_lines_db(session)
            physical_line_ids = await resolve_virtual_to_physical_db(line_id, session)
            archive_dao = self.archive_dao(session=session)
            archives = await archive_dao.get_range(from_date, to_date, physical_line_ids)

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
