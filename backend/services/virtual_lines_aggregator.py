"""
Virtual Lines Aggregator

This module handles aggregation of physical line archives into virtual lines (rings).
"""

import logging
from typing import List, Dict, Union
from datetime import datetime, date
from collections import defaultdict

from backend.db.models.hourly_archive_model import HourlyArchive, HourlyArchiveList
from backend.db.models.daily_archive_model import DailyArchive, DailyArchiveList
from backend.services.virtual_lines_config import get_active_virtual_lines

logger = logging.getLogger(__name__)


def aggregate_to_virtual_lines(
    archives: List[Union[HourlyArchiveList, DailyArchiveList]],
    requested_line_ids: List[int],
    virtual_lines: Dict = None,
) -> List[Dict]:
    """
    Aggregate physical line archives into virtual lines.

    Args:
        archives: List of archive records (HourlyArchiveList or DailyArchiveList)
        requested_line_ids: List of requested line IDs (may include virtual IDs)
        virtual_lines: Active virtual lines dict {str(id): {...}}. If None, falls
                       back to the JSON-based get_active_virtual_lines() for
                       backward compatibility.

    Returns:
        List of aggregated archive dicts with virtual line_ids

    Logic:
        1. Load virtual lines configuration
        2. Create mapping: physical_line_id -> virtual_line_id
        3. Group by (virtual_line_id, period)
        4. Aggregate:
           - volume: SUM
           - w_volume_dp: SUM
           - pressure: WEIGHTED AVG (by volume)
           - temperature: WEIGHTED AVG (by volume)
           - density: WEIGHTED AVG (by volume)
        5. Return only requested virtual lines + physical lines not in rings
    """
    if virtual_lines is None:
        virtual_lines = get_active_virtual_lines()

    # Check if any virtual lines were requested (via DB lookup)
    virtual_requested = [lid for lid in requested_line_ids if str(lid) in virtual_lines]
    physical_requested = [lid for lid in requested_line_ids if str(lid) not in virtual_lines]

    if not virtual_requested:
        # No virtual lines requested, return archives as-is (as dicts)
        return [_archive_to_dict(archive) for archive in archives]

    # Create mapping: physical_line_id -> virtual_line_id
    physical_to_virtual = {}
    for vline_id_str, vline_data in virtual_lines.items():
        vline_id = int(vline_id_str)
        for pline_id in vline_data["physical_line_ids"]:
            physical_to_virtual[pline_id] = vline_id

    # Get set of physical lines that are in rings
    physical_in_rings = set(physical_to_virtual.keys())

    # Separate archives into virtual and physical
    virtual_archives = defaultdict(lambda: defaultdict(list))
    physical_archives = []

    for archive in archives:
        line_id = archive.line_id

        if line_id in physical_to_virtual:
            # This physical line is part of a virtual line
            virtual_line_id = physical_to_virtual[line_id]
            period = archive.period
            virtual_archives[virtual_line_id][period].append(archive)
        elif line_id in physical_requested:
            # Physical line requested directly (not in any ring)
            physical_archives.append(_archive_to_dict(archive))

    # Aggregate virtual lines
    result = []

    for vline_id in virtual_requested:
        if vline_id not in virtual_archives:
            logger.warning(f"No data found for virtual line {vline_id}")
            continue

        for period, period_archives in virtual_archives[vline_id].items():
            aggregated = _aggregate_period(period_archives, vline_id, period)
            result.append(aggregated)

    # Add physical archives
    result.extend(physical_archives)

    logger.info(
        f"Aggregated {len(archives)} physical archives into "
        f"{len(result)} records ({len(virtual_requested)} virtual + {len(physical_archives)} physical)"
    )

    return result


def _aggregate_period(
    archives: List[Union[HourlyArchiveList, DailyArchiveList]],
    virtual_line_id: int,
    period: Union[datetime, date]
) -> Dict:
    """
    Aggregate multiple archives for a single period into one virtual line record.

    Args:
        archives: List of archive records for the same period
        virtual_line_id: Virtual line ID
        period: Period (datetime or date)

    Returns:
        Dict with aggregated values
    """
    total_volume = 0.0
    total_w_volume_dp = 0.0

    # For weighted averages
    weighted_pressure = 0.0
    weighted_temperature = 0.0
    weighted_density = 0.0

    for archive in archives:
        volume = archive.volume

        total_volume += volume
        total_w_volume_dp += archive.w_volume_dp

        # Weighted sums (will divide by total_volume later)
        weighted_pressure += archive.pressure * volume
        weighted_temperature += archive.temperature * volume
        weighted_density += archive.density * volume

    # Calculate weighted averages
    if total_volume > 0:
        avg_pressure = weighted_pressure / total_volume
        avg_temperature = weighted_temperature / total_volume
        avg_density = weighted_density / total_volume
    else:
        avg_pressure = 0.0
        avg_temperature = 0.0
        avg_density = 0.0

    return {
        "line_id": virtual_line_id,
        "period": period,
        "volume": total_volume,
        "w_volume_dp": total_w_volume_dp,
        "pressure": avg_pressure,
        "temperature": avg_temperature,
        "density": avg_density
    }


def _archive_to_dict(archive: Union[HourlyArchiveList, DailyArchiveList]) -> Dict:
    """
    Convert archive model to dict.

    Args:
        archive: Archive model instance

    Returns:
        Dict with archive data
    """
    return {
        "line_id": archive.line_id,
        "period": archive.period,
        "volume": archive.volume,
        "w_volume_dp": archive.w_volume_dp,
        "pressure": archive.pressure,
        "temperature": archive.temperature,
        "density": archive.density
    }
