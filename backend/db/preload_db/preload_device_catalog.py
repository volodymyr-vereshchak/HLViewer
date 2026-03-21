"""
One-time preload: hardcoded manufacturer/model mappings → DB tables.

Usage:
    python -m backend.db.preload_db.preload_device_catalog
"""
import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.engine import async_session_factory
from backend.db.models.device_catalog_model import Manufacturer, CorectorType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MANUFACTURERS = [
    {"short_name": "РадмирТех", "full_name": "РадмирТех ТОВ СП, м. Харків", "mf_dev": 1},
    {"short_name": "ГРЕМПІС",   "full_name": "ГРЕМПІС ТОВ НВП, м. Вінниця",  "mf_dev": 3},
    {"short_name": "Тандем",    "full_name": "Тандем ПП НВФ, м.Вінниця",      "mf_dev": 4},
    {"short_name": "Укргазтех", "full_name": "Укргазтех ДП, м.Київ",          "mf_dev": 5},
]

DEVICE_TYPES: dict[int, list[tuple[str, int]]] = {
    1: [  # РадмирТех
        ("ВЕГА-1.01",    5),
        ("ВЕГА-1.01Н",  15),
        ("КПЛГ-2.01Р",   4),
        ("КПЛГ-1.02Р",   2),
        ("КПЛГ-1.01Р",   2),
        ("КПЛГ-1.02РВ",  3),
        ("ВЕГА-2.01",    8),
        ("ВЕГА-2.01Н",  16),
        ("ВЕГА-1.02",    7),
        ("КВР-1.01",    11),
        ("КВР-1.02",    12),
    ],
    5: [  # Укргазтех
        ("ФЛОУТЕК-ТМ-2-3-4-Т", 12),
        ("ФЛОУТЕК-ТМ-2-3-4",   12),
        ("ФЛОУТЕК-ТМ-2-3-6",   12),
        ("ФЛОУТЕК-ТМ",         15),
        ("ФЛОУТЕК-ТМ-2",       15),
        ("ФЛОУТЕК-ТМ-1-3-1",   15),
        ("ФЛОУТЕК-ТМ-1",        9),
        ("ФЛОУТЕК-ТМ-3",        2),
    ],
    3: [  # ГРЕМПІС
        ("Універсал-02", 2),
        ("Універсал-01", 1),
    ],
    4: [  # Тандем
        ("ТАНДЕМ-ТР", 1),
    ],
}


async def preload(session: AsyncSession) -> None:
    existing = await session.execute(select(Manufacturer).limit(1))
    if existing.scalars().first():
        logger.info("manufacturer table already has data — skipping")
        return

    mf_dev_to_id: dict[int, int] = {}

    for m in MANUFACTURERS:
        mfr = Manufacturer(**m)
        session.add(mfr)
        await session.flush()
        mf_dev_to_id[m["mf_dev"]] = mfr.id
        logger.info(f"  Manufacturer: {m['short_name']} (mf_dev={m['mf_dev']}, id={mfr.id})")

    count = 0
    for mf_dev, models in DEVICE_TYPES.items():
        manufacturer_id = mf_dev_to_id[mf_dev]
        for model_name, type_dev in models:
            session.add(CorectorType(
                manufacturer_id=manufacturer_id,
                model_name=model_name,
                type_dev=type_dev,
            ))
            count += 1

    await session.commit()
    logger.info(f"Inserted {len(MANUFACTURERS)} manufacturers and {count} corector types")


async def main() -> None:
    async with async_session_factory() as session:
        await preload(session)


if __name__ == "__main__":
    asyncio.run(main())
