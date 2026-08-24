"""
Enterprise Volumes Virtual API Endpoint

Provides endpoints for fetching enterprise volume data with virtual lines support.
Aggregates physical line enterprise data into virtual lines.
"""

import logging
from typing import List
from fastapi import APIRouter, Depends, Query, status, HTTPException

from backend.api.endpoints.auth_ep import get_current_user
from backend.db.models.enterprise_models import (
    EnterpriseVolumeResponse,
    EnterpriseVolumeError
)
from backend.services.enterprise_mappings import get_devices_for_lines_db
from backend.services.enterprise_volume_service import (
    aggregate_volumes,
    fetch_dpd_volumes,
    parse_date_range,
    request_window,
)
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.engine import get_session
from backend.services.virtual_lines_config import get_active_virtual_lines_db

logger = logging.getLogger(__name__)


async def resolve_virtual_devices(
    line_id: List[int], session: AsyncSession, range_from=None, range_to=None,
) -> tuple[List[dict], dict]:
    """Resolve a mixed virtual/physical line list to enterprise devices.

    Returns (devices, physical_to_original): the line_remap for
    aggregate_volumes mapping each physical line to EVERY requested line it
    must be reported under — itself when requested directly, plus each
    requested virtual parent it belongs to. A line that is both requested
    and a virtual member contributes to both (previously the virtual parent
    silently stole its volumes, so the line itself got no enterprise data).
    Shared by the plain and streaming endpoints."""
    virtual_lines_config = await get_active_virtual_lines_db(session)

    # Separate virtual and physical line IDs (DB-backed, no numeric threshold)
    virtual_line_ids = [lid for lid in line_id if str(lid) in virtual_lines_config]
    physical_line_ids = [lid for lid in line_id if str(lid) not in virtual_lines_config]

    # physical_line_id -> [requested lines it reports to]
    physical_to_original: dict = {}
    for pline_id in physical_line_ids:
        physical_to_original.setdefault(pline_id, []).append(pline_id)
    for vline_id in virtual_line_ids:
        for pline_id in virtual_lines_config[str(vline_id)]["physical_line_ids"]:
            physical_to_original.setdefault(pline_id, []).append(vline_id)

    all_physical_ids = list(physical_to_original.keys())
    if not all_physical_ids:
        logger.info("No physical lines to query after virtual resolution")
        return [], physical_to_original

    logger.info(
        f"Resolved {len(line_id)} requested lines to "
        f"{len(all_physical_ids)} physical lines "
        f"({len(virtual_line_ids)} virtual, {len(physical_line_ids)} physical)"
    )
    devices = await get_devices_for_lines_db(
        all_physical_ids, session, range_from=range_from, range_to=range_to,
    )
    return devices, physical_to_original


class EnterpriseVirtualRouter:
    """Router for enterprise volume endpoints with virtual lines support."""

    def __init__(self):
        self.router = APIRouter(dependencies=[Depends(get_current_user)])
        self.router.add_api_route(
            path="/enterprise/volumes_virtual/",
            tags=["enterprise"],
            endpoint=self.get_enterprise_volumes_virtual,
            methods=["GET"],
            response_model=List[EnterpriseVolumeResponse],
            status_code=status.HTTP_200_OK,
            summary="Get enterprise volume data with virtual lines support",
            description=(
                "Fetches volume data for enterprise calculators from DPD API, "
                "supporting both physical and virtual lines. Virtual lines "
                "are automatically resolved to physical lines, data aggregated, and returned "
                "grouped by virtual line_id."
            ),
            responses={
                200: {
                    "description": "Successfully retrieved enterprise volumes",
                    "model": List[EnterpriseVolumeResponse]
                },
                400: {
                    "description": "Invalid request parameters",
                    "model": EnterpriseVolumeError
                },
                500: {
                    "description": "Server error (e.g., mappings file not found)",
                    "model": EnterpriseVolumeError
                },
                503: {
                    "description": "DPD API unavailable",
                    "model": EnterpriseVolumeError
                }
            }
        )

    async def get_enterprise_volumes_virtual(
        self,
        line_id: List[int] = Query(..., description="Line IDs (virtual and physical IDs supported)"),
        from_date: str = Query(..., description="Start date (YYYY-MM-DD)"),
        to_date: str = Query(..., description="End date (YYYY-MM-DD)"),
        period_type: str = Query(
            default="daily",
            pattern="^(daily|hourly)$",
            description="Data granularity: 'daily' or 'hourly'"
        ),
        include_devices: bool = Query(default=True, description="Set false to strip per-device breakdowns (line totals only)"),
        session: AsyncSession = Depends(get_session),
    ) -> List[EnterpriseVolumeResponse]:
        """
        Get enterprise volume data with virtual lines support.

        Logic:
            1. Separate virtual and physical line IDs
            2. Create mapping: physical_line_id -> original_line_id (virtual or physical)
            3. Resolve all virtual IDs to physical IDs
            4. Fetch enterprise data for all physical lines
            5. Group results by (original_line_id, period)
            6. Return aggregated data

        Args:
            line_id: List of line IDs (virtual and physical)
            from_date: Start date in YYYY-MM-DD format
            to_date: End date in YYYY-MM-DD format
            period_type: Data granularity - 'daily' (default) or 'hourly'

        Returns:
            List of EnterpriseVolumeResponse objects, each containing:
                - line_id: Gas line ID (virtual or physical)
                - period: Date (daily) or datetime (hourly) of measurement
                - total_volume: Sum of all device volumes for this line and period
                - device_count: Number of devices contributing
                - devices: List of individual device volumes

        Raises:
            HTTPException: With appropriate status code and error message
        """
        logger.info(
            f"Fetching enterprise volumes (virtual support) for lines {line_id}, "
            f"period {from_date} to {to_date}, granularity: {period_type}"
        )

        # Validate and parse dates
        try:
            date_from, date_to = parse_date_range(from_date, to_date)
        except ValueError as e:
            logger.error(f"Invalid date parameters: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid date format: {e}. Use YYYY-MM-DD format."
            )

        # Validate line_ids
        if not line_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least one line_id must be specified"
            )

        if any(lid <= 0 for lid in line_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="All line_ids must be positive integers"
            )

        # Resolve virtual lines and load device mappings (shared helper)
        try:
            win = request_window(date_from, date_to, period_type)
            devices, physical_to_original = await resolve_virtual_devices(
                line_id, session, *win,
            )
        except Exception as e:
            logger.error(f"Error resolving virtual lines / mappings: {e}")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

        if not devices:
            logger.info(f"No enterprise mappings found for lines {line_id}")
            return []

        # Fetch volumes from DPD API
        try:
            volumes_data = await fetch_dpd_volumes(devices, date_from, date_to, period_type)
        except LookupError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except Exception as e:
            logger.error(f"DPD API error: {e}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"DPD API unavailable: {e}"
            )

        if not volumes_data:
            logger.warning("No volume data returned from DPD API")
            return []

        # Aggregate by the ORIGINAL (virtual or physical) line id; devices whose
        # physical line was not requested are skipped by the remap. This endpoint
        # has always reported missing volumes as 0.0.
        result = aggregate_volumes(
            volumes_data,
            devices,
            period_type,
            line_remap=physical_to_original,
            none_volume_as_zero=True,
            include_devices=include_devices,
        )

        logger.info(
            f"Returning {len(result)} aggregated enterprise volume records "
            f"for {len(devices)} devices across {len(line_id)} requested lines"
        )

        return result


# Create router instance
enterprise_virtual_router = EnterpriseVirtualRouter().router
