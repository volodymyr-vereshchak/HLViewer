"""
Preload/sync: manufacturer/model mappings from device_catalog.json → DB tables.

Usage:
    python -m backend.db.preload_db.preload_device_catalog
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.engine import async_session_factory
from backend.db.models.device_catalog_model import Manufacturer, CorectorType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CATALOG_PATH = Path(__file__).parent / "device_catalog.json"


@dataclass
class CatalogSync:
    """What a merge did, so the admin panel can say more than "готово"."""

    added_manufacturers: int = 0
    renamed_manufacturers: int = 0
    added_models: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return (
            self.added_manufacturers
            + self.renamed_manufacturers
            + self.added_models
        )


async def preload(session: AsyncSession) -> CatalogSync:
    """Merge device_catalog.json into the DB.

    Idempotent and never destructive: rows are added or renamed in place, never
    deleted, so `corector_type.id` survives and every device that references it
    keeps working. Safe on every deploy.

    Manufacturers are identified by `mf_dev`, not by name. That is what lets a
    rename made on one installation travel to another instead of arriving as a
    duplicate: same code, new label → the label from the file wins. Note that
    this also runs on every backend start (`preload_db`), so a rename made in
    the admin panel has to be written back with «Зберегти в JSON» to survive a
    restart — same rule the other preloaded dictionaries already follow.

    Models are identified by name within the manufacturer, and an existing one
    is left exactly as it is. Their code cannot serve as identity: several
    models legitimately share one `type_dev` (ФЛОУТЕК-ТМ-2-3-4 and -2-3-4-Т are
    both 12, because the catalog has to match whatever spelling the enterprise
    Excel uses), and an admin may have added more of them on the server. So a
    name that is not in the DB is treated as a new model — a rename has to be
    made by hand on each side. A `type_dev` that disagrees with the file is
    reported and left at the DB value: it may well be the local correction.
    """
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    report = CatalogSync()

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
            report.added_manufacturers += 1
            logger.info(
                f"+ Виробник: {m['short_name']} (mf_dev={m['mf_dev']}, id={mfr.id})"
            )
        elif mfr.short_name != m["short_name"] or mfr.full_name != m["full_name"]:
            # Same mf_dev, different label — a rename made elsewhere. The code
            # is the identity, so the name from the file wins.
            logger.info(
                f"~ Виробник mf_dev={m['mf_dev']}: {mfr.short_name!r} → "
                f"{m['short_name']!r}"
            )
            mfr.short_name = m["short_name"]
            mfr.full_name = m["full_name"]
            report.renamed_manufacturers += 1

        await _sync_models(session, mfr, m, report)

    await session.commit()
    logger.info(
        f"Каталог: +{report.added_manufacturers} виробників, "
        f"~{report.renamed_manufacturers} перейменовано; "
        f"+{report.added_models} моделей"
    )
    for w in report.warnings:
        logger.warning(w)
    return report


async def _sync_models(
    session: AsyncSession, mfr: Manufacturer, payload: dict, report: CatalogSync
) -> None:
    by_name = {c.model_name: c for c in (await session.execute(
        select(CorectorType).where(CorectorType.manufacturer_id == mfr.id)
    )).scalars().all()}

    for model in payload["models"]:
        existing = by_name.get(model["model_name"])
        if existing is not None:
            if existing.type_dev != model["type_dev"]:
                report.warnings.append(
                    f"{mfr.short_name}/{model['model_name']}: код у базі "
                    f"{existing.type_dev}, у файлі {model['type_dev']} — "
                    f"залишено значення бази"
                )
            continue

        session.add(CorectorType(
            manufacturer_id=mfr.id,
            model_name=model["model_name"],
            type_dev=model["type_dev"],
        ))
        report.added_models += 1
        logger.info(
            f"  + Модель: {model['model_name']} (type_dev={model['type_dev']}) "
            f"→ {mfr.short_name}"
        )


async def main() -> None:
    async with async_session_factory() as session:
        await preload(session)


if __name__ == "__main__":
    asyncio.run(main())
