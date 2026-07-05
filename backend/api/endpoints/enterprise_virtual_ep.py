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
)
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.engine import get_session
from backend.services.virtual_lines_config import get_active_virtual_lines_db

logger = logging.getLogger(__name__)


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

        # Load virtual lines from DB
        try:
            virtual_lines_config = await get_active_virtual_lines_db(session)
        except Exception as e:
            logger.error(f"Error loading virtual lines config: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error loading virtual lines configuration: {e}"
            )

        # Separate virtual and physical line IDs (DB-backed, no numeric threshold)
        virtual_line_ids = [lid for lid in line_id if str(lid) in virtual_lines_config]
        physical_line_ids = [lid for lid in line_id if str(lid) not in virtual_lines_config]

        # Create mapping: physical_line_id -> original_line_id (virtual or physical)
        physical_to_original = {}

        # Add direct physical lines
        for pline_id in physical_line_ids:
            physical_to_original[pline_id] = pline_id

        # Map each virtual line's physical members to their virtual parent
        for vline_id in virtual_line_ids:
            vline_id_str = str(vline_id)
            for pline_id in virtual_lines_config[vline_id_str]["physical_line_ids"]:
                if pline_id in physical_to_original:
                    logger.warning(
                        f"Physical line {pline_id} is in multiple lines: "
                        f"{physical_to_original[pline_id]} and {vline_id}"
                    )
                physical_to_original[pline_id] = vline_id

        # Get all physical line IDs to query
        all_physical_ids = list(physical_to_original.keys())

        if not all_physical_ids:
            logger.info("No physical lines to query after virtual resolution")
            return []

        logger.info(f"Resolved {len(line_id)} requested lines to {len(all_physical_ids)} physical lines")

        # Load enterprise mappings for physical lines from DB
        try:
            devices = await get_devices_for_lines_db(all_physical_ids, session)
        except Exception as e:
            logger.error(f"Error loading enterprise mappings from DB: {e}")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

        if not devices:
            logger.info(f"No enterprise mappings found for lines {all_physical_ids}")
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
        )

        logger.info(
            f"Returning {len(result)} aggregated enterprise volume records "
            f"for {len(devices)} devices across {len(line_id)} requested lines "
            f"({len(virtual_line_ids)} virtual, {len(physical_line_ids)} physical)"
        )

        return result


# Create router instance
enterprise_virtual_router = EnterpriseVirtualRouter().router
