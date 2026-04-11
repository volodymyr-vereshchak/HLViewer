"""
One-time preload: manufacturer/model mappings from device_catalog.json → DB tables.

Usage:
    python -m backend.db.preload_db.preload_device_catalog
"""
import asyncio
import json
import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.engine import async_session_factory
from backend.db.models.device_catalog_model import Manufacturer, CorectorType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CATALOG_PATH = Path(__file__).parent / "device_catalog.json"


async def preload(session: AsyncSession) -> None:
    existing = await session.execute(select(Manufacturer).limit(1))
    if existing.scalars().first():
        logger.info("manufacturer table already has data — skipping")
        return

    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

    count = 0
    for m in data["manufacturers"]:
        mfr = Manufacturer(
            short_name=m["short_name"],
            full_name=m["full_name"],
            mf_dev=m["mf_dev"],
        )
        session.add(mfr)
        await session.flush()
        logger.info(f"  Manufacturer: {m['short_name']} (mf_dev={m['mf_dev']}, id={mfr.id})")

        for model in m["models"]:
            session.add(CorectorType(
                manufacturer_id=mfr.id,
                model_name=model["model_name"],
                type_dev=model["type_dev"],
            ))
            count += 1

    await session.commit()
    logger.info(f"Inserted {len(data['manufacturers'])} manufacturers and {count} corector types")


async def main() -> None:
    async with async_session_factory() as session:
        await preload(session)


if __name__ == "__main__":
    asyncio.run(main())
