"""
Virtual Lines Configuration Manager

Provides two back-ends:
  * File-based  — original JSON cache (kept for backward compat / fallback)
  * DB-backed   — async functions that query virtual_line + virtual_line_member
"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Set

from backend.settings import backend_settings

logger = logging.getLogger(__name__)

# Cache for loaded configuration
_config_cache = {
    "data": None,
    "loaded_at": None,
    "file_mtime": None
}

# Cache duration (5 minutes)
CACHE_DURATION = timedelta(minutes=5)


def load_virtual_lines(force_reload: bool = False) -> Optional[Dict]:
    """
    Load virtual lines configuration from JSON file with caching.

    Args:
        force_reload: Force reload from file, ignoring cache

    Returns:
        Dict with virtual lines configuration:
        {
            "1001": {
                "name": "Ring Name",
                "physical_line_ids": [1, 6, 22],
                "active": true,
                "description": "Description"
            },
            ...
        }

    Raises:
        FileNotFoundError: If config file doesn't exist
        ValueError: If JSON structure is invalid
    """
    global _config_cache

    # Define path to config file
    data_dir = os.path.dirname(backend_settings["ENTERPRISE_MAPPINGS_PATH"])
    config_path = os.path.join(data_dir, "virtual_lines.json")

    # Check if file exists
    if not os.path.exists(config_path):
        logger.warning(f"Virtual lines config not found: {config_path}")
        return {}

    # Get file modification time
    file_mtime = os.path.getmtime(config_path)

    # Check if cache is valid
    if not force_reload and _config_cache["data"] is not None:
        cache_age = datetime.now() - _config_cache["loaded_at"]

        if cache_age < CACHE_DURATION and _config_cache["file_mtime"] == file_mtime:
            logger.debug("Using cached virtual lines configuration")
            return _config_cache["data"]

    # Load from file
    try:
        logger.info(f"Loading virtual lines configuration from {config_path}")

        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        # Validate structure
        if "virtual_lines" not in config:
            raise ValueError("Missing 'virtual_lines' key in config")

        virtual_lines = config["virtual_lines"]

        # Validate each virtual line
        for vline_id, vline_data in virtual_lines.items():
            # Validate ID >= 1000
            try:
                vline_id_int = int(vline_id)
                if vline_id_int < 1000:
                    raise ValueError(f"Virtual line ID {vline_id} must be >= 1000")
            except ValueError as e:
                raise ValueError(f"Invalid virtual line ID '{vline_id}': {e}")

            # Validate required fields
            if "name" not in vline_data:
                raise ValueError(f"Virtual line {vline_id} missing 'name' field")
            if "physical_line_ids" not in vline_data:
                raise ValueError(f"Virtual line {vline_id} missing 'physical_line_ids' field")
            if "active" not in vline_data:
                raise ValueError(f"Virtual line {vline_id} missing 'active' field")

            # Validate physical_line_ids
            if not isinstance(vline_data["physical_line_ids"], list):
                raise ValueError(f"Virtual line {vline_id}: 'physical_line_ids' must be a list")
            if len(vline_data["physical_line_ids"]) == 0:
                raise ValueError(f"Virtual line {vline_id}: 'physical_line_ids' cannot be empty")

            # Validate physical line IDs > 0
            for pline_id in vline_data["physical_line_ids"]:
                if not isinstance(pline_id, int) or pline_id <= 0:
                    raise ValueError(
                        f"Virtual line {vline_id}: invalid physical_line_id {pline_id} (must be int > 0)"
                    )

        # Update cache
        _config_cache["data"] = virtual_lines
        _config_cache["loaded_at"] = datetime.now()
        _config_cache["file_mtime"] = file_mtime

        logger.info(f"Loaded {len(virtual_lines)} virtual lines from config")

        return virtual_lines

    except Exception as e:
        logger.error(f"Error loading virtual lines configuration: {e}")
        raise


def get_active_virtual_lines() -> Dict:
    """
    Get only active virtual lines.

    Returns:
        Dict with active virtual lines (active=True)
    """
    all_lines = load_virtual_lines()

    if not all_lines:
        return {}

    active_lines = {
        vline_id: vline_data
        for vline_id, vline_data in all_lines.items()
        if vline_data.get("active", False)
    }

    logger.debug(f"Found {len(active_lines)} active virtual lines (out of {len(all_lines)} total)")

    return active_lines


def resolve_virtual_to_physical(line_ids: List[int]) -> List[int]:
    """
    Expand virtual line IDs to physical line IDs.

    Args:
        line_ids: List of line IDs (may contain virtual IDs >= 1000)

    Returns:
        List of physical line IDs (virtual IDs expanded, physical IDs kept as-is)

    Example:
        Input: [1001, 1002, 25]
        Output: [1, 6, 22, 4, 10, 15, 25]
    """
    virtual_lines = get_active_virtual_lines()
    physical_ids = []

    for line_id in line_ids:
        if line_id >= 1000:
            # Virtual line - expand to physical
            vline_id_str = str(line_id)
            if vline_id_str in virtual_lines:
                physical_ids.extend(virtual_lines[vline_id_str]["physical_line_ids"])
                logger.debug(
                    f"Expanded virtual line {line_id} to physical lines: "
                    f"{virtual_lines[vline_id_str]['physical_line_ids']}"
                )
            else:
                logger.warning(f"Virtual line {line_id} not found in active configuration")
        else:
            # Physical line - keep as-is
            physical_ids.append(line_id)

    return physical_ids


def get_physical_lines_in_rings() -> Set[int]:
    """
    Get set of physical line IDs that are part of active virtual lines (rings).

    Returns:
        Set of physical line IDs
    """
    virtual_lines = get_active_virtual_lines()
    physical_ids = set()

    for vline_data in virtual_lines.values():
        physical_ids.update(vline_data["physical_line_ids"])

    logger.debug(f"Found {len(physical_ids)} physical lines in active rings")

    return physical_ids


def validate_config() -> Dict:
    """
    Validate virtual lines configuration.

    Returns:
        Dict with validation results:
            - valid (bool): Whether validation passed
            - virtual_lines_count (int): Number of virtual lines
            - active_count (int): Number of active virtual lines
            - hidden_physical_lines (List[int]): Physical lines that will be hidden
            - errors (List[str]): List of validation errors
            - warnings (List[str]): List of validation warnings
    """
    errors = []
    warnings = []

    try:
        all_lines = load_virtual_lines(force_reload=True)
        active_lines = get_active_virtual_lines()

        # Check for duplicate physical lines in active virtual lines
        physical_line_usage = {}
        for vline_id, vline_data in active_lines.items():
            if not vline_data.get("active", False):
                continue

            for pline_id in vline_data["physical_line_ids"]:
                if pline_id not in physical_line_usage:
                    physical_line_usage[pline_id] = []
                physical_line_usage[pline_id].append(vline_id)

        # Check for duplicates
        for pline_id, vline_ids in physical_line_usage.items():
            if len(vline_ids) > 1:
                errors.append(
                    f"Physical line {pline_id} is used in multiple active virtual lines: {vline_ids}"
                )

        # Get hidden physical lines
        hidden_lines = sorted(list(get_physical_lines_in_rings()))

        return {
            "valid": len(errors) == 0,
            "virtual_lines_count": len(all_lines),
            "active_count": len(active_lines),
            "hidden_physical_lines": hidden_lines,
            "errors": errors,
            "warnings": warnings
        }

    except Exception as e:
        return {
            "valid": False,
            "virtual_lines_count": 0,
            "active_count": 0,
            "hidden_physical_lines": [],
            "errors": [str(e)],
            "warnings": []
        }


# ─── DB-backed async functions ────────────────────────────────────────────────


async def get_active_virtual_lines_db(session) -> Dict:
    """
    Return active virtual lines from the DB as a dict compatible with
    the file-based get_active_virtual_lines() return format:

        { "<id>": { "name": ..., "physical_line_ids": [...], "active": True, ... }, ... }

    The key is the string representation of the DB primary key.
    """
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlmodel import select
    from backend.db.models.grmu_branch_model import VirtualLine, VirtualLineMember

    stmt = (
        select(VirtualLine)
        .where(VirtualLine.active == True)  # noqa: E712
    )
    result = await session.execute(stmt)
    vlines = result.scalars().all()

    out: Dict = {}
    for vl in vlines:
        # Fetch members for this virtual line
        members_stmt = (
            select(VirtualLineMember)
            .where(VirtualLineMember.virtual_line_id == vl.id)
            .order_by(VirtualLineMember.sort_order)
        )
        members_result = await session.execute(members_stmt)
        members = members_result.scalars().all()

        out[str(vl.id)] = {
            "name": vl.name,
            "physical_line_ids": [m.line_id for m in members],
            "active": vl.active,
            "description": vl.description,
            "branch_id": vl.branch_id,
        }

    logger.debug("Loaded %d active virtual lines from DB", len(out))
    return out


async def get_physical_lines_in_rings_db(session) -> Set[int]:
    """Return physical line IDs that belong to any active virtual line (DB)."""
    virtual_lines = await get_active_virtual_lines_db(session)
    physical_ids: Set[int] = set()
    for vl_data in virtual_lines.values():
        physical_ids.update(vl_data["physical_line_ids"])
    return physical_ids


async def resolve_virtual_to_physical_db(line_ids: List[int], session) -> List[int]:
    """
    Expand virtual line IDs to their physical members (DB version).

    IDs not found in the DB virtual_line table are passed through unchanged
    (treated as physical line IDs).
    """
    from sqlmodel import select
    from backend.db.models.grmu_branch_model import VirtualLine

    # Check which IDs are virtual lines in the DB
    stmt = select(VirtualLine).where(
        VirtualLine.id.in_(line_ids),
        VirtualLine.active == True,  # noqa: E712
    )
    result = await session.execute(stmt)
    db_vlines = {vl.id: vl for vl in result.scalars().all()}

    virtual_lines_full = await get_active_virtual_lines_db(session) if db_vlines else {}

    physical_ids: List[int] = []
    for lid in line_ids:
        if lid in db_vlines:
            physical_ids.extend(virtual_lines_full.get(str(lid), {}).get("physical_line_ids", []))
        else:
            physical_ids.append(lid)

    return physical_ids


async def validate_config_db(session) -> Dict:
    """Validate virtual lines configuration stored in the DB."""
    errors: List[str] = []
    warnings: List[str] = []

    try:
        all_lines_dict = await get_active_virtual_lines_db(session)

        # Check for physical lines used in multiple virtual lines
        physical_usage: Dict[int, List[str]] = {}
        for vline_id_str, vline_data in all_lines_dict.items():
            for pline_id in vline_data["physical_line_ids"]:
                physical_usage.setdefault(pline_id, []).append(vline_id_str)

        for pline_id, vline_ids in physical_usage.items():
            if len(vline_ids) > 1:
                errors.append(
                    f"Physical line {pline_id} is used in multiple active virtual lines: {vline_ids}"
                )

        hidden_lines = sorted(
            {pid for data in all_lines_dict.values() for pid in data["physical_line_ids"]}
        )

        return {
            "valid": len(errors) == 0,
            "virtual_lines_count": len(all_lines_dict),
            "active_count": len(all_lines_dict),
            "hidden_physical_lines": hidden_lines,
            "errors": errors,
            "warnings": warnings,
        }

    except Exception as e:
        return {
            "valid": False,
            "virtual_lines_count": 0,
            "active_count": 0,
            "hidden_physical_lines": [],
            "errors": [str(e)],
            "warnings": [],
        }
