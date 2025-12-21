"""
Enterprise Mappings Manager

This module handles loading and caching of Excel file containing
enterprise calculator-to-line mappings.
"""

import os
import logging
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from backend.settings import backend_settings

logger = logging.getLogger(__name__)

# Cache for loaded mappings
_mappings_cache = {
    "data": None,
    "loaded_at": None,
    "file_mtime": None
}

# Cache duration (5 minutes)
CACHE_DURATION = timedelta(minutes=5)


def load_mappings(force_reload: bool = False) -> Optional[pd.DataFrame]:
    """
    Load enterprise mappings from Excel file with caching.

    Args:
        force_reload: Force reload from file, ignoring cache

    Returns:
        pandas DataFrame with columns:
            - line_id (int)
            - serNum (int)
            - mfDev (int)
            - typeDev (int)
            - chNum (int)
            - enterprise_name (str, optional)
            - active (bool)

    Raises:
        FileNotFoundError: If Excel file doesn't exist
        ValueError: If Excel file has invalid structure
    """
    global _mappings_cache

    file_path = backend_settings["ENTERPRISE_MAPPINGS_PATH"]

    # Check if file exists
    if not os.path.exists(file_path):
        logger.error(f"Enterprise mappings file not found: {file_path}")
        raise FileNotFoundError(f"Enterprise mappings file not found: {file_path}")

    # Get file modification time
    file_mtime = os.path.getmtime(file_path)

    # Check if cache is valid
    if not force_reload and _mappings_cache["data"] is not None:
        cache_age = datetime.now() - _mappings_cache["loaded_at"]

        if cache_age < CACHE_DURATION and _mappings_cache["file_mtime"] == file_mtime:
            logger.debug("Using cached enterprise mappings")
            return _mappings_cache["data"]

    # Load from file
    try:
        logger.info(f"Loading enterprise mappings from {file_path}")

        df = pd.read_excel(file_path)

        # Validate required columns
        required_columns = ["line_id", "serNum", "mfDev", "typeDev", "chNum", "active"]
        missing_columns = set(required_columns) - set(df.columns)

        if missing_columns:
            raise ValueError(
                f"Excel file missing required columns: {missing_columns}. "
                f"Required: {required_columns}"
            )

        # Ensure correct data types
        df["line_id"] = df["line_id"].astype(int)
        df["serNum"] = df["serNum"].astype(int)
        df["mfDev"] = df["mfDev"].astype(int)
        df["typeDev"] = df["typeDev"].astype(int)
        df["chNum"] = df["chNum"].astype(int)

        # Convert active to boolean (handle TRUE/FALSE strings)
        if df["active"].dtype == "object":
            df["active"] = df["active"].map({
                "TRUE": True, "True": True, "true": True, 1: True,
                "FALSE": False, "False": False, "false": False, 0: False
            })
        else:
            df["active"] = df["active"].astype(bool)

        # Add enterprise_name column if missing
        if "enterprise_name" not in df.columns:
            df["enterprise_name"] = ""

        # Update cache
        _mappings_cache["data"] = df
        _mappings_cache["loaded_at"] = datetime.now()
        _mappings_cache["file_mtime"] = file_mtime

        logger.info(
            f"Loaded {len(df)} enterprise mappings "
            f"({len(df[df['active']])} active)"
        )

        return df

    except Exception as e:
        logger.error(f"Error loading enterprise mappings: {e}")
        raise


def get_devices_for_lines(line_ids: List[int]) -> List[Dict]:
    """
    Get active devices mapped to specified lines.

    Args:
        line_ids: List of line IDs to filter by

    Returns:
        List of device dicts, each containing:
            - line_id (int)
            - serNum (int)
            - mfDev (int)
            - typeDev (int)
            - chNum (int)
            - enterprise_name (str)

    Raises:
        FileNotFoundError: If mappings file doesn't exist
        ValueError: If mappings file has invalid structure
    """
    df = load_mappings()

    if df is None or df.empty:
        logger.warning("No enterprise mappings available")
        return []

    # Filter by line_ids and active status
    filtered = df[
        (df["line_id"].isin(line_ids)) &
        (df["active"] == True)
    ]

    if filtered.empty:
        logger.info(f"No active enterprise mappings found for lines: {line_ids}")
        return []

    # Convert to list of dicts
    devices = filtered[[
        "line_id", "serNum", "mfDev", "typeDev", "chNum", "enterprise_name"
    ]].to_dict("records")

    logger.info(f"Found {len(devices)} active devices for {len(line_ids)} lines")

    return devices


def validate_mappings() -> Dict[str, any]:
    """
    Validate enterprise mappings file.

    Returns:
        Dict with validation results:
            - valid (bool): Whether validation passed
            - total_mappings (int): Total number of mappings
            - active_mappings (int): Number of active mappings
            - lines_covered (List[int]): Unique line IDs covered
            - errors (List[str]): List of validation errors
    """
    errors = []

    try:
        df = load_mappings(force_reload=True)

        # Check for duplicate devices
        device_cols = ["serNum", "mfDev", "typeDev", "chNum"]
        duplicates = df[df.duplicated(subset=device_cols, keep=False)]

        if not duplicates.empty:
            errors.append(
                f"Found {len(duplicates)} duplicate device entries"
            )

        # Check for invalid values
        if (df["line_id"] <= 0).any():
            errors.append("Found line_id values <= 0")

        if (df["serNum"] < 0).any():
            errors.append("Found negative serNum values")

        if (df["mfDev"] < 0).any():
            errors.append("Found negative mfDev values")

        if (df["typeDev"] < 0).any():
            errors.append("Found negative typeDev values")

        if (df["chNum"] < 0).any():
            errors.append("Found negative chNum values")

        return {
            "valid": len(errors) == 0,
            "total_mappings": len(df),
            "active_mappings": len(df[df["active"]]),
            "lines_covered": sorted(df["line_id"].unique().tolist()),
            "errors": errors
        }

    except Exception as e:
        return {
            "valid": False,
            "total_mappings": 0,
            "active_mappings": 0,
            "lines_covered": [],
            "errors": [str(e)]
        }
