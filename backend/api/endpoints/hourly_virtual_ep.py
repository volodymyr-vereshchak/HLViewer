"""
Hourly Virtual Lines API Endpoint

Endpoint for querying hourly archives with virtual lines support.
"""

from datetime import datetime
from fastapi import APIRouter, status, Query
from typing import List

from backend.db.engine import async_session_factory
from backend.db.dao.hourly_archive_dao import HourlyArchiveDao
from backend.services.virtual_lines_config import resolve_virtual_to_physical
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
                "Virtual line IDs (>= 1000) are automatically aggregated from their constituent physical lines."
            )
        )

    async def get_archive(
        self,
        from_date: datetime = Query(None, description="Start date/time"),
        to_date: datetime = Query(None, description="End date/time"),
        line_id: List[int] = Query(None, description="List of line IDs (virtual IDs >= 1000 supported)")
    ):
        """
        Get hourly archives with virtual lines support.

        Args:
            from_date: Start datetime
            to_date: End datetime
            line_id: List of line IDs (may include virtual IDs >= 1000)

        Returns:
            List of archive records (physical or aggregated virtual)

        Logic:
            1. Resolve virtual line IDs to physical line IDs
            2. Query database for physical line archives
            3. If virtual lines were requested, aggregate them
            4. Return results
        """
        if not line_id:
            line_id = []

        # Resolve virtual line IDs to physical
        physical_line_ids = resolve_virtual_to_physical(line_id)

        # Query database for physical lines
        async with async_session_factory() as session:
            archive_dao = HourlyArchiveDao(session=session)
            archives = await archive_dao.get_range(from_date, to_date, physical_line_ids)

        # Check if any virtual lines were requested
        has_virtual = any(lid >= 1000 for lid in line_id)

        if has_virtual:
            # Aggregate to virtual lines
            aggregated = aggregate_to_virtual_lines(archives, line_id)
            return aggregated
        else:
            # Return physical archives as-is (convert to dicts)
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
