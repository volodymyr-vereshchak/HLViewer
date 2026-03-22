"""
Hourly Virtual Lines API Endpoint

Endpoint for querying hourly archives with virtual lines support.
"""

from datetime import datetime
from fastapi import APIRouter, HTTPException, status, Query
from typing import List

from backend.db.engine import async_session_factory
from backend.db.dao.hourly_archive_dao import HourlyArchiveDao
from backend.services.virtual_lines_config import (
    get_active_virtual_lines_db,
    resolve_virtual_to_physical_db,
)
from backend.services.virtual_lines_aggregator import aggregate_to_virtual_lines


class HourlyVirtualRouter:
    """Router for hourly archives with virtual lines support."""

    def __init__(self):
        self.router = APIRouter()

        self.router.add_api_route(
            path="/hourly_virtual/",
            tags=["hourly_virtual"],
            endpoint=self.get_archive,
            methods=["GET"],
            response_model=List[dict],
            status_code=status.HTTP_200_OK,
            summary="Get hourly archives with virtual lines support",
            description=(
                "Returns hourly archive data supporting both physical and virtual lines. "
                "Virtual line IDs are automatically aggregated from their constituent physical lines."
            )
        )

    async def get_archive(
        self,
        from_date: datetime = Query(None, description="Start date/time"),
        to_date: datetime = Query(None, description="End date/time"),
        line_id: List[int] = Query(None, description="List of line IDs (virtual IDs supported)")
    ):
        if not from_date or not to_date:
            raise HTTPException(status_code=400, detail="from_date and to_date are required")
        if (to_date - from_date).days > 90:
            raise HTTPException(status_code=400, detail="Date range exceeds 90 days")

        if not line_id:
            line_id = []

        async with async_session_factory() as session:
            # Load virtual lines config from DB
            virtual_lines = await get_active_virtual_lines_db(session)
            # Resolve virtual line IDs to physical
            physical_line_ids = await resolve_virtual_to_physical_db(line_id, session)
            # Query database for physical lines
            archive_dao = HourlyArchiveDao(session=session)
            archives = await archive_dao.get_range(from_date, to_date, physical_line_ids)

        # Check if any virtual lines were requested (DB-backed)
        has_virtual = any(str(lid) in virtual_lines for lid in line_id)

        if has_virtual:
            return aggregate_to_virtual_lines(archives, line_id, virtual_lines=virtual_lines)
        else:
            return [
                {
                    "line_id": archive.line_id,
                    "period": archive.period,
                    "volume": archive.volume,
                    "w_volume_dp": archive.w_volume_dp,
                    "pressure": archive.pressure,
                    "temperature": archive.temperature,
                    "density": archive.density
                }
                for archive in archives
            ]


hourly_virtual_router = HourlyVirtualRouter().router
