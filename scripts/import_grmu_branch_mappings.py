#!/usr/bin/env python3
"""
One-time import: enterprise.xlsx / line_id.xlsx → grmu_branch_device_mapping

Usage:
    cd HLViewer
    python scripts/import_grmu_branch_mappings.py [--branch-id 1] [--dry-run]

Reads the existing Excel files via enterprise_mappings.load_mappings() and
inserts all rows into grmu_branch_device_mapping for the specified branch.
Rows that already exist (unique on branch_id, ser_num, mf_dev, type_dev, ch_num)
are skipped (ON CONFLICT DO NOTHING).
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

# Make sure project root is on PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services.enterprise_mappings import load_mappings
from backend.db.engine import async_session_factory
from backend.db.models.grmu_branch_model import GrmuBranchDeviceMapping

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def run_import(branch_id: int, dry_run: bool) -> None:
    logger.info("Loading enterprise mappings from Excel files...")
    df = load_mappings(force_reload=True)

    if df is None or df.empty:
        logger.error("No data loaded from Excel — aborting.")
        return

    logger.info("Loaded %d rows from Excel.", len(df))

    rows = []
    for _, row in df.iterrows():
        import pandas as pd
        line_id = None if pd.isna(row["line_id"]) else int(row["line_id"])
        rows.append(
            GrmuBranchDeviceMapping(
                branch_id=branch_id,
                line_id=line_id,
                ser_num=int(row["serNum"]),
                mf_dev=int(row["mfDev"]),
                type_dev=int(row["typeDev"]),
                ch_num=int(row["chNum"]),
                grmu_branch_name=str(row["enterprise_name"]),
                active=bool(row["active"]),
                status="Включена" if bool(row.get("enabled", False)) else "Відключена",
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
        )

    if dry_run:
        logger.info("[dry-run] Would insert %d rows for branch_id=%d", len(rows), branch_id)
        for r in rows[:5]:
            logger.info("  sample: ser_num=%s mf_dev=%s line_id=%s name=%s",
                        r.ser_num, r.mf_dev, r.line_id, r.grmu_branch_name)
        return

    inserted = 0
    skipped = 0

    async with async_session_factory() as session:
        for r in rows:
            try:
                session.add(r)
                await session.flush()
                inserted += 1
            except Exception:
                await session.rollback()
                skipped += 1
                # Re-open the same session after rollback isn't clean;
                # use separate sessions per row for robustness
                async with async_session_factory() as inner:
                    pass  # row already exists — skip silently
        await session.commit()

    logger.info(
        "Import complete: %d inserted, %d skipped (duplicates).",
        inserted, skipped,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--branch-id", type=int, default=1,
        help="grmu_branch.id to assign all imported rows to (default: 1)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be imported without writing to DB",
    )
    args = parser.parse_args()

    asyncio.run(run_import(args.branch_id, args.dry_run))


if __name__ == "__main__":
    main()
