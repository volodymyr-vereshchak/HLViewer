"""
Enterprise Volumes Virtual API Endpoint

Provides endpoints for fetching enterprise volume data with virtual lines support.
Aggregates physical line enterprise data into virtual lines.
"""

import logging
from datetime import datetime, date
from typing import List
from collections import defaultdict
from fastapi import APIRouter, Query, status, HTTPException

from backend.db.models.enterprise_models import (
    EnterpriseVolumeResponse,
    DeviceVolume,
    EnterpriseVolumeError
)
from backend.services.dpd_client import DPDClient
from backend.services.enterprise_mappings import get_devices_for_lines
from backend.db.engine import async_session_factory
from backend.services.virtual_lines_config import get_active_virtual_lines_db

logger = logging.getLogger(__name__)


class EnterpriseVirtualRouter:
    """Router for enterprise volume endpoints with virtual lines support."""

    def __init__(self):
        self.router = APIRouter()
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
        )
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
            date_from = datetime.strptime(from_date, "%Y-%m-%d")
            date_to = datetime.strptime(to_date, "%Y-%m-%d")

            if date_from > date_to:
                raise ValueError("from_date must be <= to_date")

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
            async with async_session_factory() as session:
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

        # Load enterprise mappings for physical lines
        try:
            devices = get_devices_for_lines(all_physical_ids)
        except FileNotFoundError as e:
            logger.error(f"Enterprise mappings file not found: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(e)
            )
        except Exception as e:
            logger.error(f"Error loading enterprise mappings: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error loading enterprise mappings: {e}"
            )

        if not devices:
            logger.info(f"No enterprise mappings found for lines {all_physical_ids}")
            return []

        # Fetch volumes from DPD API
        try:
            client = DPDClient()
            volumes_data = await client.get_volumes(
                devices, date_from, date_to, type_request=period_type
            )
        except Exception as e:
            logger.error(f"DPD API error: {e}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"DPD API unavailable: {e}"
            )

        if not volumes_data:
            logger.warning(f"No volume data returned from DPD API")
            return []

        # Create device lookup map for metadata
        device_map = {
            (d["serNum"], d["mfDev"], d["typeDev"], d["chNum"]): d
            for d in devices
        }

        # Aggregate volumes by (original_line_id, period)
        # Structure: {(original_line_id, period): {"total": float, "devices": []}}
        aggregated = defaultdict(lambda: {"total": 0.0, "devices": []})

        for record in volumes_data:
            # Get device metadata from mappings
            device_key = (
                record.get("serNum"),
                record.get("mfDev"),
                record.get("typeDev"),
                record.get("chNum")
            )

            device_info = device_map.get(device_key)
            if not device_info:
                logger.warning(f"Device not found in mappings: {device_key}")
                continue

            # Get physical line_id from device
            physical_line_id = device_info["line_id"]

            # Map to original line_id (virtual or physical)
            original_line_id = physical_to_original.get(physical_line_id)
            if not original_line_id:
                # This physical line was not requested
                logger.debug(f"Physical line {physical_line_id} not in requested lines, skipping")
                continue

            # Extract volume (dvstAlwrk = standard volume)
            volume = record.get("dvstAlwrk", 0.0)
            if volume is None:
                volume = 0.0

            # Parse period from record based on type
            record_date_str = record.get("date") or record.get("period")
            if not record_date_str:
                logger.warning(f"No date field in record: {record}")
                continue

            try:
                # For hourly data, preserve full datetime; for daily, use date only
                if period_type == 'hourly':
                    # Parse as datetime and preserve it
                    if isinstance(record_date_str, str):
                        # DPD API returns datetime in format "YYYY-MM-DDTHH:MM" or "YYYY-MM-DDTHH:MM:SS"
                        # Remove microseconds if present
                        clean_str = record_date_str.split(".")[0]

                        # Try parsing with seconds first, then without
                        try:
                            record_period = datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S")
                        except ValueError:
                            # Try without seconds (format: "2025-12-01T07:00")
                            record_period = datetime.strptime(clean_str, "%Y-%m-%dT%H:%M")
                    elif isinstance(record_date_str, datetime):
                        record_period = record_date_str
                    else:
                        logger.warning(f"Invalid datetime format: {record_date_str}")
                        continue
                else:
                    # Daily data - use date only
                    if isinstance(record_date_str, str):
                        record_period = datetime.strptime(
                            record_date_str.split("T")[0], "%Y-%m-%d"
                        ).date()
                    elif isinstance(record_date_str, datetime):
                        # If DPD returns datetime object, extract date only
                        record_period = record_date_str.date()
                    elif isinstance(record_date_str, date):
                        record_period = record_date_str
                    else:
                        logger.warning(f"Invalid date format: {record_date_str}")
                        continue
            except Exception as e:
                logger.warning(f"Error parsing period {record_date_str}: {e}")
                continue

            # Aggregate by (original_line_id, period)
            key = (original_line_id, record_period)

            aggregated[key]["total"] += volume
            aggregated[key]["devices"].append(
                DeviceVolume(
                    serNum=device_info["serNum"],
                    mfDev=device_info["mfDev"],
                    typeDev=device_info["typeDev"],
                    chNum=device_info["chNum"],
                    enterprise_name=device_info.get("enterprise_name", ""),
                    volume=volume
                )
            )

        # Convert aggregated data to response format
        result = []
        for (line_id_val, period_val), data in aggregated.items():
            result.append(
                EnterpriseVolumeResponse(
                    line_id=line_id_val,  # Virtual or physical
                    period=period_val,
                    total_volume=round(data["total"], 2),
                    device_count=len(data["devices"]),
                    devices=data["devices"]
                )
            )

        # Sort by line_id and period
        result.sort(key=lambda x: (x.line_id, x.period))

        logger.info(
            f"Returning {len(result)} aggregated enterprise volume records "
            f"for {len(devices)} devices across {len(line_id)} requested lines "
            f"({len(virtual_line_ids)} virtual, {len(physical_line_ids)} physical)"
        )

        return result


# Create router instance
enterprise_virtual_router = EnterpriseVirtualRouter().router
