"""
Shared pipeline for the enterprise volume endpoints.

/enterprise/volumes/ and /enterprise/volumes_virtual/ used to carry two ~150-line
copies of the same flow: parse the date range, resolve devices, call the DPD API
for one branch, then aggregate device records into per-(line, period) responses.
The only real differences are captured by two parameters:

- ``line_remap``: physical line_id → reported line_id (virtual parent). None
  keeps the device's own line_id (plain endpoint); with a mapping, devices whose
  line is not in it are skipped (they were not requested).
- ``none_volume_as_zero``: the virtual endpoint has always reported missing
  volumes as 0.0, the plain one preserves None (the frontend shows a gap).
"""

import logging
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, List, Optional

from backend.db.engine import async_session_factory
from backend.db.models.enterprise_models import DeviceVolume, EnterpriseVolumeResponse
from backend.services.dpd_client import DPDClient
from backend.services.enterprise_mappings import volume_field_for_device

logger = logging.getLogger(__name__)


def parse_date_range(from_date: str, to_date: str) -> tuple[datetime, datetime]:
    """Parse YYYY-MM-DD query params (tolerating time suffixes). Raises
    ValueError on bad format or from_date > to_date."""
    date_from = datetime.strptime(from_date.split(" ")[0].split("T")[0], "%Y-%m-%d")
    date_to = datetime.strptime(to_date.split(" ")[0].split("T")[0], "%Y-%m-%d")
    if date_from > date_to:
        raise ValueError("from_date must be <= to_date")
    return date_from, date_to


def pick_branch_id(devices: List[Dict]) -> Optional[int]:
    """DPD credentials are per branch; all requested devices belong to one."""
    return next((d["branch_id"] for d in devices if d.get("branch_id")), None)


async def fetch_dpd_volumes(
    devices: List[Dict],
    date_from: datetime,
    date_to: datetime,
    period_type: str,
) -> List[Dict]:
    """Load branch credentials and fetch raw volume records from the DPD API.

    Raises LookupError when no device carries a branch_id, ValueError when the
    branch has no/incomplete credentials (DPDClient.for_branch), and whatever
    the HTTP client raises when the API is unreachable."""
    branch_id = pick_branch_id(devices)
    if branch_id is None:
        raise LookupError("Could not determine branch for requested lines")

    async with async_session_factory() as cred_session:
        client = await DPDClient.for_branch(branch_id, cred_session)
    return await client.get_volumes(devices, date_from, date_to, type_request=period_type)


def _parse_record_period(raw, period_type: str):
    """Normalize the DPD record timestamp: datetime for hourly, date for daily.
    Returns None (record skipped) on an unparseable value."""
    try:
        if period_type == "hourly":
            if isinstance(raw, str):
                clean = raw.split(".")[0]  # strip microseconds
                try:
                    return datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    return datetime.strptime(clean, "%Y-%m-%dT%H:%M")
            if isinstance(raw, datetime):
                return raw
        else:
            if isinstance(raw, str):
                return datetime.strptime(raw.split("T")[0], "%Y-%m-%d").date()
            if isinstance(raw, datetime):
                return raw.date()
            if isinstance(raw, date):
                return raw
    except Exception as e:
        logger.warning(f"Error parsing period {raw}: {e}")
        return None
    logger.warning(f"Invalid period value: {raw!r}")
    return None


def aggregate_volumes(
    volumes_data: List[Dict],
    devices: List[Dict],
    period_type: str,
    line_remap: Optional[Dict[int, int]] = None,
    none_volume_as_zero: bool = False,
) -> List[EnterpriseVolumeResponse]:
    """Group raw DPD records into per-(line_id, period) responses."""
    device_map = {
        (d["serNum"], d["mfDev"], d["typeDev"], d["chNum"]): d for d in devices
    }
    aggregated = defaultdict(lambda: {"total": 0.0, "devices": []})

    for record in volumes_data:
        device_key = (
            record.get("serNum"),
            record.get("mfDev"),
            record.get("typeDev"),
            record.get("chNum"),
        )
        device_info = device_map.get(device_key)
        if not device_info:
            logger.warning(f"Device not found in mappings: {device_key}")
            continue

        line_key = device_info["line_id"]
        if line_remap is not None:
            line_key = line_remap.get(line_key)
            if line_key is None:
                logger.debug(
                    f"Physical line {device_info['line_id']} not in requested lines, skipping"
                )
                continue

        # Most devices report the commercial volume in dvstAlwrk; a few models
        # (ТКБ, smart104) report it in dvwrkAlwrk — selected by device identity.
        volume = record.get(
            volume_field_for_device(device_info["mfDev"], device_info["typeDev"])
        )
        if volume is None and none_volume_as_zero:
            volume = 0.0

        raw_period = record.get("date") or record.get("period")
        if not raw_period:
            logger.warning(f"No date field in record: {record}")
            continue
        record_period = _parse_record_period(raw_period, period_type)
        if record_period is None:
            continue

        pressure_unit = record.get("pressUnit")
        if isinstance(pressure_unit, str):
            pressure_unit = pressure_unit.strip() or None

        key = (line_key, record_period)
        if volume is not None:
            aggregated[key]["total"] += volume
        aggregated[key]["devices"].append(
            DeviceVolume(
                serNum=device_info["serNum"],
                mfDev=device_info["mfDev"],
                typeDev=device_info["typeDev"],
                chNum=device_info["chNum"],
                enterprise_name=device_info.get("enterprise_name", ""),
                volume=volume,
                temperature=record.get("temper"),
                pressure=record.get("press"),
                pressure_unit=pressure_unit,
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
    result.sort(key=lambda x: (x.line_id is None, x.line_id or 0, x.period))
    return result
