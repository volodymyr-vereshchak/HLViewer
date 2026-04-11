"""
Export device catalog (manufacturers + models) from DB → device_catalog.json.

Use this after editing data via the admin panel to save changes back to JSON.

Usage:
    python -m backend.db.preload_db.export_device_catalog
"""
import asyncio
import json
import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, asc

from backend.db.engine import async_session_factory
from backend.db.models.device_catalog_model import Manufacturer, CorectorType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CATALOG_PATH = Path(__file__).parent / "device_catalog.json"


async def export(session: AsyncSession) -> None:
    manufacturers = (
        await session.execute(select(Manufacturer).order_by(asc(Manufacturer.mf_dev)))
    ).scalars().all()

    result = {"manufacturers": []}

    for mfr in manufacturers:
        models = (
            await session.execute(
                select(CorectorType)
                .where(CorectorType.manufacturer_id == mfr.id)
                .order_by(asc(CorectorType.id))
            )
        ).scalars().all()

        result["manufacturers"].append({
            "short_name": mfr.short_name,
            "full_name":  mfr.full_name,
            "mf_dev":     mfr.mf_dev,
            "models": [
                {"model_name": m.model_name, "type_dev": m.type_dev}
                for m in models
            ],
        })

    CATALOG_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"Exported {len(manufacturers)} manufacturers to {CATALOG_PATH}")


async def main() -> None:
    async with async_session_factory() as session:
        await export(session)


if __name__ == "__main__":
    asyncio.run(main())
