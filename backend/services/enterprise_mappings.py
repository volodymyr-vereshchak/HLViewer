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

from backend.services import device_history
from backend.settings import backend_settings

logger = logging.getLogger(__name__)

# Manufacturer device mapping (from DPD loader.py)
MF_DEV_MAPPING = {
    "РадмирТех ТОВ СП, м. Харків": 1,
    "Укргазтех ДП, м.Київ": 5,
    "ГРЕМПІС ТОВ НВП, м. Вінниця": 3,
    "Тандем ПП НВФ, м.Вінниця": 4,
}

# Type device mapping (from DPD loader.py)
TYPE_DEV_MAPPING = {
    1: {  # РадмирТех
        "ВЕГА-1.01": 5,
        "ВЕГА-1.01Н": 15,
        "КПЛГ-2.01Р": 4,
        "КПЛГ-1.02Р": 2,
        "КПЛГ-1.01Р": 2,
        "КПЛГ-1.02РВ": 3,
        "ВЕГА-2.01": 8,
        "ВЕГА-2.01Н": 16,
        "ВЕГА-1.02": 7,
        "КВР-1.01": 11,
        "КВР-1.02": 12,
    },
    5: {  # Укргазтех
        "ФЛОУТЕК-ТМ-2-3-4-Т": 12,
        "ФЛОУТЕК-ТМ-2-3-4": 12,
        "ФЛОУТЕК-ТМ-2-3-6": 12,
        "ФЛОУТЕК-ТМ": 15,
        "ФЛОУТЕК-ТМ-2": 15,
        "ФЛОУТЕК-ТМ-1-3-1": 15,
        "ФЛОУТЕК-ТМ-1": 9,
        "ФЛОУТЕК-ТМ-3": 2
    },
    3: {  # ГРЕМПІС
        "Універсал-02": 2,
        "Універсал-01": 1,
    },
    4: {  # Тандем
        "ТАНДЕМ-ТР": 1,
    },
}

# Corrector device types whose commercial volume is reported by DPD in the
# dvwrkAlwrk field (work volume) instead of the usual dvstAlwrk (standard
# volume). Keyed by the effective DPD device identity (mf_dev, type_dev):
#   (1, 24)  → Радміртех «ТКБ»
#   (11, 1)  → Львівгаз(RGC) «smart104»
WORK_VOLUME_DEVICES = {(1, 24), (11, 1)}


def volume_field_for_device(mf_dev, type_dev) -> str:
    """DPD volume field to read for a given device identity.

    Most correctors report the commercial volume in dvstAlwrk; a few models
    (see WORK_VOLUME_DEVICES) report it in dvwrkAlwrk instead.
    """
    return "dvwrkAlwrk" if (mf_dev, type_dev) in WORK_VOLUME_DEVICES else "dvstAlwrk"


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
    Load enterprise mappings from source Excel files with caching.

    Data sources:
    - backend/data/enterprise.xlsx - enterprise and device data
    - backend/data/line_id.xlsx - sector to line_id mapping

    Args:
        force_reload: Force reload from files, ignoring cache

    Returns:
        pandas DataFrame with columns:
            - line_id (int)
            - serNum (int)
            - mfDev (int)
            - typeDev (int)
            - chNum (int)
            - enterprise_name (str)
            - active (bool)

    Raises:
        FileNotFoundError: If source files don't exist
        ValueError: If file structure is invalid
    """
    global _mappings_cache

    # Define paths to source files
    data_dir = os.path.dirname(backend_settings["ENTERPRISE_MAPPINGS_PATH"])
    enterprise_path = os.path.join(data_dir, "enterprise.xlsx")
    line_id_path = os.path.join(data_dir, "line_id.xlsx")

    # Check if both files exist
    if not os.path.exists(enterprise_path):
        logger.error(f"Enterprise file not found: {enterprise_path}")
        raise FileNotFoundError(f"Enterprise file not found: {enterprise_path}")

    if not os.path.exists(line_id_path):
        logger.error(f"Line ID file not found: {line_id_path}")
        raise FileNotFoundError(f"Line ID file not found: {line_id_path}")

    # Get file modification times
    enterprise_mtime = os.path.getmtime(enterprise_path)
    line_id_mtime = os.path.getmtime(line_id_path)
    combined_mtime = (enterprise_mtime, line_id_mtime)

    # Check if cache is valid
    if not force_reload and _mappings_cache["data"] is not None:
        cache_age = datetime.now() - _mappings_cache["loaded_at"]

        if cache_age < CACHE_DURATION and _mappings_cache["file_mtime"] == combined_mtime:
            logger.debug("Using cached enterprise mappings")
            return _mappings_cache["data"]

    # Load from files
    try:
        logger.info(f"Loading enterprise mappings from {enterprise_path} and {line_id_path}")

        # Load source files
        enterprise_df = pd.read_excel(enterprise_path, skiprows=3)
        line_id_df = pd.read_excel(line_id_path, header=None)  # No header row in line_id.xlsx

        # Process line_id.xlsx - create sector to line_id mapping
        line_id_df.columns = ["sector_name", "line_id"]
        sector_to_line = line_id_df.set_index("sector_name")["line_id"].to_dict()

        # Transform enterprise.xlsx data
        df = enterprise_df.copy()

        # serNum: Clean from "-" and remove leading zeros
        df["serNum"] = df["Прилад обліку.Номер приладу"].astype(str).str.replace("-", "", regex=False).str.lstrip("0")

        # mfDev: Get from MF_DEV_MAPPING by manufacturer
        df["mfDev"] = df["Прилад обліку.Тип приладу обліку.Виробник"].map(MF_DEV_MAPPING)

        # typeDev: Get from TYPE_DEV_MAPPING using mfDev and device type
        df["typeDev"] = df.apply(
            lambda row: TYPE_DEV_MAPPING.get(row["mfDev"], {}).get(row["Прилад обліку.Тип приладу обліку"])
            if pd.notna(row["mfDev"]) else None,
            axis=1
        )

        # chNum: Subtract 1 from channel number (0-based indexing, like in loader.py)
        df["chNum"] = df["Прилад обліку.Номер каналу (нумерація з 1)"] - 1

        # enterprise_name: Get enterprise name
        df["enterprise_name"] = df["Точка обліку"]

        # line_id: Get through sector mapping
        df["line_id"] = df["Точка обліку.Сектор"].map(sector_to_line)

        # active: Get from Active column (1 = active, 0 = inactive)
        df["active"] = df["Active"] == 1

        # enabled: Get from "Статус точки обліку" column ("Включена" = enabled)
        df["enabled"] = df["Статус точки обліку"] == "Включена"

        # Log records without line_id mapping (kept for display, not used for polling)
        missing_line_count = df["line_id"].isna().sum()
        if missing_line_count > 0:
            logger.info(f"{missing_line_count} records without line_id mapping (will be shown as 'without line')")
            missing_enterprises = df[df["line_id"].isna()]["enterprise_name"].unique()
            logger.debug(f"Enterprises without line_id: {', '.join(missing_enterprises[:10])}")

        # Remove rows without mfDev or typeDev
        invalid_mapping = df[df["mfDev"].isna() | df["typeDev"].isna()]
        if len(invalid_mapping) > 0:
            logger.warning(f"Skipped {len(invalid_mapping)} records with unknown mfDev/typeDev")
            for _, row in invalid_mapping.head(5).iterrows():
                logger.debug(
                    f"Unknown mapping for {row['enterprise_name']}: "
                    f"manufacturer={row.get('Прилад обліку.Тип приладу обліку.Виробник')}, "
                    f"device={row.get('Прилад обліку.Тип приладу обліку')}"
                )

        df = df.dropna(subset=["mfDev", "typeDev"])

        # Select and order columns
        result_df = df[["line_id", "serNum", "mfDev", "typeDev", "chNum", "enterprise_name", "active", "enabled"]].copy()

        # Convert data types (line_id can be NaN for unmapped enterprises)
        result_df["line_id"] = result_df["line_id"].astype("Int64")  # nullable integer
        result_df["serNum"] = result_df["serNum"].astype(int)
        result_df["mfDev"] = result_df["mfDev"].astype(int)
        result_df["typeDev"] = result_df["typeDev"].astype(int)
        result_df["chNum"] = result_df["chNum"].astype(int)
        result_df["active"] = result_df["active"].astype(bool)
        result_df["enabled"] = result_df["enabled"].astype(bool)

        # Update cache
        _mappings_cache["data"] = result_df
        _mappings_cache["loaded_at"] = datetime.now()
        _mappings_cache["file_mtime"] = combined_mtime

        logger.info(
            f"Loaded {len(result_df)} enterprise mappings from source files "
            f"({len(result_df[result_df['active']])} active)"
        )

        return result_df

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


async def _query_assignments_db(
    session, *where, range_from=None, range_to=None
) -> list[dict]:
    """Shared ASSIGNMENT-dict builder for the DB-backed lookups below.

    One dict per history entry, not per enterprise: a metering point that has
    had two correctors contributes two, each carrying the window it was in
    force for. Callers READ per window, which is what keeps a replaced
    corrector's data out of the previous device's periods; polling collapses
    the list to distinct devices and ignores the windows entirely.

    Device codes are read THROUGH the corrector-type catalog when the device
    is linked (so catalog edits propagate to DPD polling), falling back to the
    legacy mf_dev/type_dev columns for not-yet-linked rows.

    With `range_from`/`range_to` (inclusive datetimes) only assignments that
    overlap the range are returned. Their windows are NOT rewritten: `win_to`
    stays the true, EXCLUSIVE end (None = still in force), because callers
    test records against it with `covers()`. Clipping to the request is done
    where an inclusive span is actually wanted — the archive read.
    """
    from sqlmodel import select
    from backend.db.models.enterprise_model import (
        DpdDevice, Enterprise, EnterpriseDevice,
    )
    from backend.db.models.device_catalog_model import CorectorType, Manufacturer

    stmt = (
        select(
            Enterprise, EnterpriseDevice, DpdDevice,
            CorectorType.type_dev, Manufacturer.mf_dev,
        )
        .join(EnterpriseDevice, EnterpriseDevice.enterprise_id == Enterprise.id)
        .join(DpdDevice, DpdDevice.id == EnterpriseDevice.device_id)
        .outerjoin(CorectorType, DpdDevice.corector_type_id == CorectorType.id)
        .outerjoin(Manufacturer, CorectorType.manufacturer_id == Manufacturer.id)
        .where(Enterprise.active == True)  # noqa: E712
    )
    for clause in where:
        stmt = stmt.where(clause)
    rows = (await session.execute(stmt)).all()

    # Windows are derived per point, so the history of each has to be whole
    # before any of it can be filtered by range.
    by_enterprise: dict[int, list] = {}
    context: dict[int, tuple] = {}
    for ent, assignment, device, ct_type_dev, mfr_mf_dev in rows:
        by_enterprise.setdefault(ent.id, []).append(assignment)
        context[assignment.id] = (ent, device, ct_type_dev, mfr_mf_dev)

    assignments = []
    for ent_id, entries in by_enterprise.items():
        for assignment, win_from, win_to in device_history.resolve_windows(entries):
            ent, device, ct_type_dev, mfr_mf_dev = context[assignment.id]
            linked = device.corector_type_id is not None
            eff_mf = mfr_mf_dev if linked else device.mf_dev
            eff_type = ct_type_dev if linked else device.type_dev
            # No resolvable device identity (unlinked AND no legacy codes) →
            # cannot be addressed on the DPD API at all.
            if eff_mf is None or eff_type is None:
                continue
            if range_from is not None and device_history.clip(
                win_from, win_to, range_from, range_to
            ) is None:
                continue
            assignments.append({
                "enterprise_id": ent_id,
                "assignment_id": assignment.id,
                "device_id": device.id,
                # Effective line id: physical or DPD line (ids never collide —
                # shared_line_id_seq), so consumers group by one key either way.
                "line_id": ent.line_id if ent.line_id is not None else ent.dpd_line_id,
                "branch_id": ent.branch_id,
                "serNum": device.ser_num,
                "mfDev": eff_mf,
                "typeDev": eff_type,
                "chNum": device.ch_num,
                "enterprise_name": ent.enterprise_name,
                "win_from": win_from,
                "win_to": win_to,
            })
    return assignments


async def get_devices_for_lines_db(
    line_ids: list[int], session, range_from=None, range_to=None
) -> list[dict]:
    """Assignments of the active enterprises on the given lines (physical or
    DPD), optionally narrowed and clipped to a datetime range."""
    from sqlalchemy import or_
    from backend.db.models.enterprise_model import Enterprise

    return await _query_assignments_db(
        session,
        or_(
            Enterprise.line_id.in_(line_ids),
            Enterprise.dpd_line_id.in_(line_ids),
        ),
        range_from=range_from,
        range_to=range_to,
    )


async def get_assignments_for_device_db(
    ser_num: int, mf_dev: int, type_dev: int, ch_num: int, session,
    range_from=None, range_to=None,
) -> list[dict]:
    """Assignments of one corrector, addressed the way the DPD API addresses
    it. A device that moved has several — narrow with a range to get the
    point(s) it actually served then."""
    from sqlalchemy import or_
    from backend.db.models.enterprise_model import DpdDevice
    from backend.db.models.device_catalog_model import CorectorType, Manufacturer

    return await _query_assignments_db(
        session,
        DpdDevice.ser_num == ser_num,
        DpdDevice.ch_num == ch_num,
        # Effective codes: the catalog when linked, the legacy columns
        # otherwise — the same rule the poll resolves identity by.
        or_(Manufacturer.mf_dev == mf_dev, DpdDevice.mf_dev == mf_dev),
        or_(CorectorType.type_dev == type_dev, DpdDevice.type_dev == type_dev),
        range_from=range_from,
        range_to=range_to,
    )


async def get_devices_for_branch_db(
    branch_id: int, session, range_from=None, range_to=None
) -> list[dict]:
    """All assignments of a branch's active enterprises — the scheduler
    refresh works branch-by-branch (DPD credentials are per branch)."""
    from backend.db.models.enterprise_model import Enterprise

    return await _query_assignments_db(
        session,
        Enterprise.branch_id == branch_id,
        range_from=range_from,
        range_to=range_to,
    )


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

        # Check for invalid values (skip NaN line_ids - those are valid unmapped enterprises)
        valid_line_ids = df["line_id"].dropna()
        if (valid_line_ids <= 0).any():
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
            "lines_covered": sorted(df["line_id"].dropna().unique().tolist()),
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
