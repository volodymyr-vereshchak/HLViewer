"""
GrmuBranch Device Mappings Service (DB-backed)

Replaces enterprise_mappings.py Excel-based loading with queries against
the grmu_branch_device_mapping table.
"""

import logging
from typing import Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.models.grmu_branch_model import GrmuBranchDeviceMapping

logger = logging.getLogger(__name__)


async def get_devices_for_lines(
    line_ids: List[int],
    session: AsyncSession,
    branch_id: Optional[int] = None,
) -> List[Dict]:
    """
    Get active devices mapped to the given physical line IDs.

    Args:
        line_ids:   Physical line IDs to filter by.
        session:    Async DB session.
        branch_id:  Optional branch filter (limits scope to one enterprise).

    Returns:
        List of device dicts with keys:
            line_id, ser_num, mf_dev, type_dev, ch_num,
            grmu_branch_name (enterprise name), branch_id
    """
    if not line_ids:
        return []

    stmt = select(GrmuBranchDeviceMapping).where(
        GrmuBranchDeviceMapping.line_id.in_(line_ids),
        GrmuBranchDeviceMapping.active == True,  # noqa: E712
    )
    if branch_id is not None:
        stmt = stmt.where(GrmuBranchDeviceMapping.branch_id == branch_id)

    result = await session.execute(stmt)
    rows = result.scalars().all()

    devices = [
        {
            "line_id": row.line_id,
            "serNum": row.ser_num,
            "mfDev": row.mf_dev,
            "typeDev": row.type_dev,
            "chNum": row.ch_num,
            "enterprise_name": row.grmu_branch_name or "",
            "branch_id": row.branch_id,
        }
        for row in rows
    ]

    logger.info(
        "Found %d active device mappings for %d lines%s",
        len(devices),
        len(line_ids),
        f" (branch_id={branch_id})" if branch_id else "",
    )
    return devices


async def get_all_mappings(
    session: AsyncSession,
    branch_id: Optional[int] = None,
) -> List[GrmuBranchDeviceMapping]:
    """
    Return all device mapping rows, optionally filtered by branch.
    """
    stmt = select(GrmuBranchDeviceMapping)
    if branch_id is not None:
        stmt = stmt.where(GrmuBranchDeviceMapping.branch_id == branch_id)
    stmt = stmt.order_by(
        GrmuBranchDeviceMapping.branch_id,
        GrmuBranchDeviceMapping.line_id,
        GrmuBranchDeviceMapping.ser_num,
    )
    result = await session.execute(stmt)
    return result.scalars().all()
