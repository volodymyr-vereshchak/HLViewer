"""
Enterprise Volumes API Endpoint

Provides endpoints for fetching enterprise volume data from DPD API
and aggregating by line_id and date.
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

logger = logging.getLogger(__name__)


class EnterpriseRouter:
    """Router for enterprise volume endpoints."""

    def __init__(self):
        self.router = APIRouter()
        self.router.add_api_route(
            path="/enterprise/volumes/",
            tags=["enterprise"],
            endpoint=self.get_enterprise_volumes,
            methods=["GET"],
            response_model=List[EnterpriseVolumeResponse],
            status_code=status.HTTP_200_OK,
            summary="Get enterprise volume data",
            description=(
                "Fetches volume data for enterprise calculators from DPD API, "
                "aggregated by line_id and date. Returns empty array if no "
                "mappings exist for specified lines."
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

    async def get_enterprise_volumes(
        self,
        line_id: List[int] = Query(..., description="Line IDs to fetch volumes for"),
        from_date: str = Query(..., description="Start date (YYYY-MM-DD)"),
        to_date: str = Query(..., description="End date (YYYY-MM-DD)"),
        period_type: str = Query(
            default="daily",
            pattern="^(daily|hourly)$",
            description="Data granularity: 'daily' or 'hourly'"
        )
    ) -> List[EnterpriseVolumeResponse]:
        """
        Get enterprise volume data aggregated by line_id and time period.

        Args:
            line_id: List of line IDs to fetch volumes for
            from_date: Start date in YYYY-MM-DD format
            to_date: End date in YYYY-MM-DD format
            period_type: Data granularity - 'daily' (default) or 'hourly'

        Returns:
            List of EnterpriseVolumeResponse objects, each containing:
                - line_id: Gas line ID
                - period: Date (daily) or datetime (hourly) of measurement
                - total_volume: Sum of all device volumes for this line and period
                - device_count: Number of devices contributing
                - devices: List of individual device volumes

        Raises:
            HTTPException: With appropriate status code and error message
        """
        logger.info(
            f"Fetching enterprise volumes for lines {line_id}, "
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

        # Load enterprise mappings
        try:
            devices = get_devices_for_lines(line_id)
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

        # If no mappings found, return empty array (not an error)
        if not devices:
            logger.info(f"No enterprise mappings found for lines {line_id}")
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

        # Aggregate volumes by line_id and date
        # Structure: {(line_id, date): {"total": float, "devices": []}}
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

            # Extract volume (dvstAlwrk = standard volume)
            volume = record.get("dvstAlwrk", 0.0)
            if volume is None:
                volume = 0.0

            # Parse period from record based on type
            # DPD API might return "date" or "period" field
            record_date_str = record.get("date") or record.get("period")
            if not record_date_str:
                logger.warning(f"No date field in record: {record}")
                continue

            try:
                # For hourly data, preserve full datetime; for daily, use date only
                if period_type == 'hourly':
                    # Parse as datetime and preserve it
                    if isinstance(record_date_str, str):
                        # DPD API returns datetime in format "YYYY-MM-DDTHH:MM:SS"
                        record_period = datetime.strptime(
                            record_date_str.split(".")[0],  # Remove microseconds if present
                            "%Y-%m-%dT%H:%M:%S"
                        )
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
                    elif isinstance(record_date_str, date):
                        record_period = record_date_str
                    else:
                        logger.warning(f"Invalid date format: {record_date_str}")
                        continue
            except Exception as e:
                logger.warning(f"Error parsing period {record_date_str}: {e}")
                continue

            # Aggregate by line_id and period (date or datetime depending on type)
            key = (device_info["line_id"], record_period)

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
                    line_id=line_id_val,
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
            f"for {len(devices)} devices"
        )

        return result


# Create router instance
enterprise_router = EnterpriseRouter().router
