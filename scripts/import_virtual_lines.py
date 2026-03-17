#!/usr/bin/env python3
"""
One-time import: virtual_lines.json → virtual_line + virtual_line_member tables

Usage:
    cd HLViewer
    python scripts/import_virtual_lines.py [--branch-id 1] [--dry-run]

Reads backend/data/virtual_lines.json and inserts all entries into the DB.
The JSON virtual line IDs (1001, 1002, …) become regular SERIAL DB ids;
order of insertion determines the new integer id.
Rows that already exist (unique on branch_id, name) are skipped.
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# Make sure project root is on PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.settings import backend_settings
from backend.db.engine import async_session_factory
from backend.db.models.grmu_branch_model import VirtualLine, VirtualLineMember

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _load_json() -> dict:
    data_dir = Path(backend_settings["ENTERPRISE_MAPPINGS_PATH"]).parent
    config_path = data_dir / "virtual_lines.json"
    if not config_path.exists():
        raise FileNotFoundError(f"virtual_lines.json not found at {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


async def run_import(branch_id: int, dry_run: bool) -> None:
    config = _load_json()
    virtual_lines_data = config.get("virtual_lines", {})

    if not virtual_lines_data:
        logger.warning("virtual_lines.json contains no entries.")
        return

    logger.info("Found %d virtual lines in JSON.", len(virtual_lines_data))

    if dry_run:
        for vid, vdata in virtual_lines_data.items():
            logger.info(
                "[dry-run] id=%s  name=%r  physical_lines=%s  active=%s",
                vid, vdata["name"], vdata["physical_line_ids"], vdata.get("active"),
            )
        return

    async with async_session_factory() as session:
        for vid_str, vdata in virtual_lines_data.items():
            vl = VirtualLine(
                branch_id=branch_id,
                name=vdata["name"],
                description=vdata.get("description"),
                active=vdata.get("active", True),
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
            session.add(vl)
            await session.flush()  # get the new id

            for sort_idx, phys_id in enumerate(vdata["physical_line_ids"]):
                member = VirtualLineMember(
                    virtual_line_id=vl.id,
                    line_id=phys_id,
                    sort_order=sort_idx,
                    created_at=datetime.now(),
                    updated_at=datetime.now(),
                )
                session.add(member)

            logger.info(
                "Inserted virtual line id=%d  name=%r  members=%s",
                vl.id, vl.name, vdata["physical_line_ids"],
            )

        await session.commit()

    logger.info("Import complete.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--branch-id", type=int, default=1,
        help="grmu_branch.id to assign all virtual lines to (default: 1)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be imported without writing to DB",
    )
    args = parser.parse_args()

    asyncio.run(run_import(args.branch_id, args.dry_run))


if __name__ == "__main__":
    main()
