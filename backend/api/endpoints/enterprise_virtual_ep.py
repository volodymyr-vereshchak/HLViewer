"""
Enterprise Volumes Virtual API Endpoint

Provides endpoints for fetching enterprise volume data with virtual lines support.
Aggregates physical line enterprise data into virtual lines.
"""

import asyncio
import json
import logging
from datetime import datetime, date
from typing import List
from collections import defaultdict
from fastapi import APIRouter, Depends, Query, status, HTTPException
from fastapi.responses import StreamingResponse

from backend.api.endpoints.auth_ep import get_current_user
from backend.db.models.enterprise_models import (
    EnterpriseVolumeResponse,
    DeviceVolume,
    EnterpriseVolumeError
)
from backend.services.dpd_client import DPDClient
from backend.services.enterprise_mappings import get_devices_for_lines, get_devices_for_lines_db, volume_field_for_device
from backend.db.engine import async_session_factory
from backend.services.virtual_lines_config import get_active_virtual_lines_db

logger = logging.getLogger(__name__)


async def _resolve_virtual_devices(line_id: List[int], from_date: str, to_date: str):
    """Validate params and resolve requested (virtual/physical) lines to the set
    of enterprise devices to poll.

    Returns (devices, physical_to_original, date_from, date_to). `devices` is
    empty when there is nothing to poll. Raises HTTPException on bad input.
    """
    try:
        date_from = datetime.strptime(from_date.split(" ")[0].split("T")[0], "%Y-%m-%d")
        date_to = datetime.strptime(to_date.split(" ")[0].split("T")[0], "%Y-%m-%d")
        if date_from > date_to:
            raise ValueError("from_date must be <= to_date")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Invalid date format: {e}. Use YYYY-MM-DD format.")

    if not line_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="At least one line_id must be specified")
    if any(lid <= 0 for lid in line_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="All line_ids must be positive integers")

    try:
        async with async_session_factory() as session:
            virtual_lines_config = await get_active_virtual_lines_db(session)
    except Exception as e:
        logger.error(f"Error loading virtual lines config: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"Error loading virtual lines configuration: {e}")

    virtual_line_ids = [lid for lid in line_id if str(lid) in virtual_lines_config]
    physical_line_ids = [lid for lid in line_id if str(lid) not in virtual_lines_config]

    # Map each physical line back to the original (virtual or physical) line it belongs to.
    physical_to_original = {pid: pid for pid in physical_line_ids}
    for vline_id in virtual_line_ids:
        for pline_id in virtual_lines_config[str(vline_id)]["physical_line_ids"]:
            physical_to_original[pline_id] = vline_id

    all_physical_ids = list(physical_to_original.keys())
    if not all_physical_ids:
        return [], {}, date_from, date_to

    try:
        async with async_session_factory() as session:
            devices = await get_devices_for_lines_db(all_physical_ids, session)
    except Exception as e:
        logger.error(f"Error loading enterprise mappings from DB: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    return devices, physical_to_original, date_from, date_to


def _aggregate_virtual(volumes_data, devices, physical_to_original, period_type: str) -> List[EnterpriseVolumeResponse]:
    """Aggregate raw DPD volume records into per-(original line, period) totals."""
    device_map = {
        (d["serNum"], d["mfDev"], d["typeDev"], d["chNum"]): d
        for d in devices
    }
    aggregated = defaultdict(lambda: {"total": 0.0, "devices": []})

    for record in volumes_data:
        device_key = (record.get("serNum"), record.get("mfDev"), record.get("typeDev"), record.get("chNum"))
        device_info = device_map.get(device_key)
        if not device_info:
            logger.warning(f"Device not found in mappings: {device_key}")
            continue

        original_line_id = physical_to_original.get(device_info["line_id"])
        if not original_line_id:
            continue

        # Most devices report volume in dvstAlwrk; a few models (ТКБ, smart104)
        # report it in dvwrkAlwrk — selected by device identity (mfDev, typeDev).
        volume = record.get(volume_field_for_device(device_info["mfDev"], device_info["typeDev"]), 0.0)
        if volume is None:
            volume = 0.0

        record_date_str = record.get("date") or record.get("period")
        if not record_date_str:
            logger.warning(f"No date field in record: {record}")
            continue

        try:
            if period_type == 'hourly':
                if isinstance(record_date_str, str):
                    clean_str = record_date_str.split(".")[0]
                    try:
                        record_period = datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S")
                    except ValueError:
                        record_period = datetime.strptime(clean_str, "%Y-%m-%dT%H:%M")
                elif isinstance(record_date_str, datetime):
                    record_period = record_date_str
                else:
                    logger.warning(f"Invalid datetime format: {record_date_str}")
                    continue
            else:
                if isinstance(record_date_str, str):
                    record_period = datetime.strptime(record_date_str.split("T")[0], "%Y-%m-%d").date()
                elif isinstance(record_date_str, datetime):
                    record_period = record_date_str.date()
                elif isinstance(record_date_str, date):
                    record_period = record_date_str
                else:
                    logger.warning(f"Invalid date format: {record_date_str}")
                    continue
        except Exception as e:
            logger.warning(f"Error parsing period {record_date_str}: {e}")
            continue

        key = (original_line_id, record_period)
        aggregated[key]["total"] += volume
        aggregated[key]["devices"].append(
            DeviceVolume(
                serNum=device_info["serNum"],
                mfDev=device_info["mfDev"],
                typeDev=device_info["typeDev"],
                chNum=device_info["chNum"],
                enterprise_name=device_info.get("enterprise_name", ""),
                volume=volume,
            )
        )

    result = [
        EnterpriseVolumeResponse(
            line_id=line_id_val,
            period=period_val,
            total_volume=round(data["total"], 2),
            device_count=len(data["devices"]),
            devices=data["devices"],
        )
        for (line_id_val, period_val), data in aggregated.items()
    ]
    result.sort(key=lambda x: (x.line_id, x.period))
    return result


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
        self.router.add_api_route(
            path="/enterprise/volumes_virtual/stream",
            tags=["enterprise"],
            endpoint=self.stream_enterprise_volumes_virtual,
            methods=["GET"],
            status_code=status.HTTP_200_OK,
            summary="Stream enterprise volume data with live per-device progress",
            description=(
                "Same data as /enterprise/volumes_virtual/ but streamed as NDJSON: "
                "emits progress events {done,total} as each enterprise device is polled, "
                "then a final result event. Lets the UI show a live % of enterprises polled."
            ),
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

        devices, physical_to_original, date_from, date_to = await _resolve_virtual_devices(
            line_id, from_date, to_date
        )
        if not devices:
            logger.info(f"No enterprise mappings found for requested lines {line_id}")
            return []

        branch_id = next((d["branch_id"] for d in devices if d.get("branch_id")), None)
        if branch_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not determine branch for requested lines"
            )

        try:
            async with async_session_factory() as cred_session:
                client = await DPDClient.for_branch(branch_id, cred_session)
            volumes_data = await client.get_volumes(
                devices, date_from, date_to, type_request=period_type
            )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except Exception as e:
            logger.error(f"DPD API error: {e}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"DPD API unavailable: {e}"
            )

        result = _aggregate_virtual(volumes_data, devices, physical_to_original, period_type)
        logger.info(
            f"Returning {len(result)} aggregated enterprise volume records "
            f"for {len(devices)} devices across {len(line_id)} requested lines"
        )
        return result

    async def stream_enterprise_volumes_virtual(
        self,
        line_id: List[int] = Query(..., description="Line IDs (virtual and physical IDs supported)"),
        from_date: str = Query(..., description="Start date (YYYY-MM-DD)"),
        to_date: str = Query(..., description="End date (YYYY-MM-DD)"),
        period_type: str = Query(default="daily", pattern="^(daily|hourly)$"),
    ):
        """NDJSON stream of the same data with a live per-device progress feed.

        Emits one JSON object per line:
          {"type":"progress","done":k,"total":N}   after each device is polled
          {"type":"result","data":[...]}            final aggregated records
          {"type":"error","detail":"..."}           on DPD/branch failure
        """
        devices, physical_to_original, date_from, date_to = await _resolve_virtual_devices(
            line_id, from_date, to_date
        )

        async def event_stream():
            total = len(devices)
            if total == 0:
                yield json.dumps({"type": "result", "data": []}) + "\n"
                return

            branch_id = next((d["branch_id"] for d in devices if d.get("branch_id")), None)
            if branch_id is None:
                yield json.dumps({"type": "error", "detail": "Could not determine branch for requested lines"}) + "\n"
                return

            async with async_session_factory() as cred_session:
                client = await DPDClient.for_branch(branch_id, cred_session)

            queue: asyncio.Queue = asyncio.Queue()

            async def cb(done, tot):
                await queue.put(("progress", done, tot))

            async def run():
                try:
                    vd = await client.get_volumes(
                        devices, date_from, date_to, type_request=period_type, progress_cb=cb
                    )
                    await queue.put(("result", vd))
                except Exception as e:
                    logger.error(f"DPD API error (stream): {e}")
                    await queue.put(("error", str(e)))

            task = asyncio.create_task(run())
            # Prime the bar at 0/total before the first device finishes.
            yield json.dumps({"type": "progress", "done": 0, "total": total}) + "\n"
            try:
                while True:
                    item = await queue.get()
                    kind = item[0]
                    if kind == "progress":
                        yield json.dumps({"type": "progress", "done": item[1], "total": item[2]}) + "\n"
                    elif kind == "result":
                        result = _aggregate_virtual(item[1], devices, physical_to_original, period_type)
                        data = [
                            {
                                "line_id": r.line_id,
                                "period": r.period.isoformat(),
                                "total_volume": r.total_volume,
                                "device_count": r.device_count,
                                "devices": [d.model_dump() for d in r.devices],
                            }
                            for r in result
                        ]
                        yield json.dumps({"type": "result", "data": data}) + "\n"
                        break
                    elif kind == "error":
                        yield json.dumps({"type": "error", "detail": item[1]}) + "\n"
                        break
            finally:
                await task

        return StreamingResponse(
            event_stream(),
            media_type="application/x-ndjson",
            # Tell nginx not to buffer so progress reaches the browser live.
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


# Create router instance
enterprise_virtual_router = EnterpriseVirtualRouter().router
