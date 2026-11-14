"""
Preload/sync: manufacturer/model mappings from device_catalog.json → DB tables.

Usage:
    python -m backend.db.preload_db.preload_device_catalog
"""
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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
    renamed_models: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return (
            self.added_manufacturers
            + self.renamed_manufacturers
            + self.added_models
            + self.renamed_models
        )


def _uid(entry: dict) -> Optional[uuid.UUID]:
    raw = entry.get("uid")
    return uuid.UUID(raw) if raw else None


async def preload(session: AsyncSession) -> CatalogSync:
    """Merge device_catalog.json into the DB.

    Idempotent and never destructive: rows are added or renamed in place, never
    deleted, so `corector_type.id` survives and every device that references it
    keeps working. Safe on every deploy.

    An entry is identified by its `uid` (`export_uid` in the DB): minted once,
    written into the file by the export, the same on every installation. That
    is what lets a rename made on one server travel to another instead of
    arriving as a duplicate — of a manufacturer and of a model alike: same
    uid, new name → the name from the file wins. Note that this also runs on
    every backend start (`preload_db`), so a rename made in the admin panel
    has to be written back with «Зберегти в JSON» to survive a restart — same
    rule the other preloaded dictionaries already follow.

    Where the uid finds nothing — a file written before uids, an entry typed
    in by hand on both sides — the old keys still apply: a manufacturer by its
    `mf_dev` (or by short name where it has no code), a model by its name
    within the manufacturer. A row matched that way takes the uid of the file,
    so the next merge matches directly.

    Codes are never rewritten from the file. Several models legitimately share
    one `type_dev` (ФЛОУТЕК-ТМ-2-3-4 and -2-3-4-Т are both 12), and a code that
    disagrees with the file may well be the local correction: it is reported
    and left at the DB value.
    """
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    report = CatalogSync()

    for m in data["manufacturers"]:
        code = m.get("mf_dev")
        uid = _uid(m)
        mfr = None
        if uid is not None:
            mfr = (await session.execute(
                select(Manufacturer).where(Manufacturer.export_uid == uid)
            )).scalars().first()
        if mfr is None:
            mfr = (await session.execute(
                select(Manufacturer).where(
                    Manufacturer.mf_dev == code if code is not None
                    else Manufacturer.short_name == m["short_name"]
                )
            )).scalars().first()
            if mfr is not None and uid is not None:
                mfr.export_uid = uid

        if mfr is None:
            mfr = Manufacturer(
                short_name=m["short_name"],
                full_name=m["full_name"],
                mf_dev=code,
                **({"export_uid": uid} if uid is not None else {}),
            )
            session.add(mfr)
            await session.flush()
            report.added_manufacturers += 1
            logger.info(
                f"+ Виробник: {m['short_name']} (mf_dev={code}, id={mfr.id})"
            )
        else:
            if mfr.short_name != m["short_name"] or mfr.full_name != m["full_name"]:
                logger.info(
                    f"~ Виробник mf_dev={mfr.mf_dev}: {mfr.short_name!r} → "
                    f"{m['short_name']!r}"
                )
                mfr.short_name = m["short_name"]
                mfr.full_name = m["full_name"]
                report.renamed_manufacturers += 1
            if mfr.mf_dev != code:
                report.warnings.append(
                    f"{mfr.short_name}: код mf_dev у базі {mfr.mf_dev}, у файлі "
                    f"{code} — залишено значення бази"
                )

        await _sync_models(session, mfr, m, report)

    await session.commit()
    logger.info(
        f"Каталог: +{report.added_manufacturers} виробників, "
        f"~{report.renamed_manufacturers} перейменовано; "
        f"+{report.added_models} моделей, ~{report.renamed_models} перейменовано"
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
        type_dev = model.get("type_dev")
        name = model["model_name"]
        uid = _uid(model)

        existing = None
        if uid is not None:
            existing = (await session.execute(
                select(CorectorType).where(CorectorType.export_uid == uid)
            )).scalars().first()
        if existing is None:
            existing = by_name.get(name)
            if existing is not None and uid is not None:
                existing.export_uid = uid

        if existing is not None:
            if existing.model_name != name:
                if name in by_name:
                    # Both spellings already live here as two rows. Which one
                    # the devices should hang off is the admin's call.
                    report.warnings.append(
                        f"{mfr.short_name}: модель «{existing.model_name}» у файлі "
                        f"названо «{name}», але така модель тут уже є окремо — "
                        f"назву не змінено"
                    )
                else:
                    logger.info(f"  ~ Модель: {existing.model_name!r} → {name!r}")
                    by_name.pop(existing.model_name, None)
                    existing.model_name = name
                    by_name[name] = existing
                    report.renamed_models += 1
            if existing.type_dev != type_dev:
                report.warnings.append(
                    f"{mfr.short_name}/{name}: код у базі "
                    f"{existing.type_dev}, у файлі {type_dev} — "
                    f"залишено значення бази"
                )
            continue

        created = CorectorType(
            manufacturer_id=mfr.id,
            model_name=name,
            type_dev=type_dev,
            **({"export_uid": uid} if uid is not None else {}),
        )
        session.add(created)
        by_name[name] = created
        report.added_models += 1
        logger.info(
            f"  + Модель: {name} (type_dev={type_dev}) → {mfr.short_name}"
        )


async def main() -> None:
    async with async_session_factory() as session:
        await preload(session)


if __name__ == "__main__":
    asyncio.run(main())
