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
    """Incremental merge of device_catalog.json into the DB.

    Idempotent and non-destructive: adds manufacturers/models that are missing,
    leaves existing rows (and their ids) untouched so enterprise FK links
    (enterprise.corector_type_id) stay valid. Runs safely on every deploy.

    Match keys: manufacturer by mf_dev (stable DPD code, unique), model by
    (manufacturer_id, model_name). Existing records are NOT updated — only new
    ones are inserted; a type_dev mismatch on an existing model is logged.
    """
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

    added_mfr = 0
    added_models = 0

    for m in data["manufacturers"]:
        mfr = (await session.execute(
            select(Manufacturer).where(Manufacturer.mf_dev == m["mf_dev"])
        )).scalars().first()

        if mfr is None:
            mfr = Manufacturer(
                short_name=m["short_name"],
                full_name=m["full_name"],
                mf_dev=m["mf_dev"],
            )
            session.add(mfr)
            await session.flush()
            added_mfr += 1
            logger.info(f"+ Manufacturer: {m['short_name']} (mf_dev={m['mf_dev']}, id={mfr.id})")

        for model in m["models"]:
            existing = (await session.execute(
                select(CorectorType).where(
                    CorectorType.manufacturer_id == mfr.id,
                    CorectorType.model_name == model["model_name"],
                )
            )).scalars().first()

            if existing is not None:
                if existing.type_dev != model["type_dev"]:
                    logger.warning(
                        f"! Model '{model['model_name']}' ({m['short_name']}) type_dev "
                        f"differs: DB={existing.type_dev} JSON={model['type_dev']} — left as DB value"
                    )
                continue

            session.add(CorectorType(
                manufacturer_id=mfr.id,
                model_name=model["model_name"],
                type_dev=model["type_dev"],
            ))
            added_models += 1
            logger.info(f"  + Model: {model['model_name']} (type_dev={model['type_dev']}) → {m['short_name']}")

    await session.commit()
    logger.info(f"Device catalog merge: +{added_mfr} manufacturers, +{added_models} models (existing untouched)")


async def main() -> None:
    async with async_session_factory() as session:
        await preload(session)


if __name__ == "__main__":
    asyncio.run(main())
