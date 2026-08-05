"""
One-time preload: Excel enterprise.xlsx + line_id.xlsx → enterprise DB table.

Usage (inside container):
    python -m backend.db.preload_db.preload_enterprise
"""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.dao.enterprise_dao import EnterpriseDao
from backend.db.engine import async_session_factory
from backend.db.models.enterprise_model import (
    Enterprise, EnterpriseDevice, EPOCH_INSTALLED_FROM,
)
from backend.services.enterprise_mappings import load_mappings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def preload(session: AsyncSession) -> None:
    # Check if table already has data
    existing = await session.execute(select(Enterprise).limit(1))
    if existing.scalars().first():
        logger.info("enterprise table already has data — skipping preload")
        return

    logger.info("Loading enterprise mappings from Excel...")
    df = load_mappings()

    if df is None or df.empty:
        logger.warning("No enterprise mappings found in Excel — nothing to preload")
        return

    import pandas as pd

    dao = EnterpriseDao(session)
    count = 0
    for _, row in df.iterrows():
        line_id = None if pd.isna(row["line_id"]) else int(row["line_id"])
        ent = Enterprise(
            enterprise_name=str(row["enterprise_name"]),
            line_id=line_id,
            active=bool(row["active"]),
            enabled=bool(row["enabled"]),
        )
        session.add(ent)
        await session.flush()
        # The Excel carries raw DPD codes, not catalog ids, so the device is
        # created on the legacy mf_dev/type_dev fallback — the same state the
        # catalog backfill migration leaves unmatched rows in.
        device = await dao.get_or_create_device(
            ser_num=int(row["serNum"]),
            corector_type_id=None,
            ch_num=int(row["chNum"]),
            mf_dev=int(row["mfDev"]),
            type_dev=int(row["typeDev"]),
        )
        # No install date in this source: the point has always had this
        # corrector, which is what the epoch means.
        session.add(EnterpriseDevice(
            enterprise_id=ent.id,
            device_id=device.id,
            installed_from=EPOCH_INSTALLED_FROM,
        ))
        count += 1

    await session.commit()
    logger.info(f"Inserted {count} enterprise records into DB")


async def main() -> None:
    async with async_session_factory() as session:
        await preload(session)


if __name__ == "__main__":
    asyncio.run(main())
