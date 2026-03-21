"""
One-time preload: Excel enterprise.xlsx + line_id.xlsx → enterprise DB table.

Usage (inside container):
    python -m backend.db.preload_db.preload_enterprise
"""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.engine import async_session_factory
from backend.db.models.enterprise_model import Enterprise, EnterpriseCreate
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

    records: list[Enterprise] = []
    for _, row in df.iterrows():
        import pandas as pd
        line_id = None if pd.isna(row["line_id"]) else int(row["line_id"])
        records.append(
            Enterprise(
                enterprise_name=str(row["enterprise_name"]),
                line_id=line_id,
                ser_num=int(row["serNum"]),
                mf_dev=int(row["mfDev"]),
                type_dev=int(row["typeDev"]),
                ch_num=int(row["chNum"]),
                active=bool(row["active"]),
                enabled=bool(row["enabled"]),
            )
        )

    session.add_all(records)
    await session.commit()
    logger.info(f"Inserted {len(records)} enterprise records into DB")


async def main() -> None:
    async with async_session_factory() as session:
        await preload(session)


if __name__ == "__main__":
    asyncio.run(main())
